"""
English to Malayalam PDF Translator
A user-friendly web application for translating PDF documents from English to Malayalam.
"""

import os
import uuid
import time
import threading
import textwrap
import requests
import pdfplumber
from flask import Flask, render_template, request, send_file, jsonify
from deep_translator import GoogleTranslator
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

# Approximate ratio of character width to font point size for Malayalam/Latin text.
# Used to estimate how many characters fit on one line when wrapping.
CHAR_WIDTH_RATIO = 0.55

# ── App setup ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "outputs")
FONT_DIR = os.path.join(BASE_DIR, "fonts")
FONT_PATH = os.path.join(FONT_DIR, "NotoSansMalayalam-Regular.ttf")
FONT_URL = (
    "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/"
    "NotoSansMalayalam/NotoSansMalayalam-Regular.ttf"
)
ALLOWED_EXTENSIONS = {"pdf"}

for folder in (UPLOAD_FOLDER, OUTPUT_FOLDER, FONT_DIR):
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

# In-memory job store: job_id -> {status, progress, message, output_file}
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()  # Protects concurrent reads/writes to `jobs`


# ── Font helper ────────────────────────────────────────────────────────────────
def ensure_malayalam_font() -> bool:
    """Download the Malayalam font if it is not already present."""
    if os.path.exists(FONT_PATH):
        return True
    print("Downloading Malayalam font (one-time setup)…")
    try:
        resp = requests.get(FONT_URL, timeout=60)
        resp.raise_for_status()
        with open(FONT_PATH, "wb") as fh:
            fh.write(resp.content)
        print("Font downloaded successfully.")
        return True
    except Exception as exc:
        print(f"Font download failed: {exc}")
        return False


def register_font() -> bool:
    """Register the Malayalam font with ReportLab (idempotent)."""
    if "NotoMalayalam" in pdfmetrics.getRegisteredFontNames():
        return True
    if not ensure_malayalam_font():
        return False
    pdfmetrics.registerFont(TTFont("NotoMalayalam", FONT_PATH))
    return True


# ── Translation helper ─────────────────────────────────────────────────────────
# Google Translate's unofficial limit is ~5000 characters; we use 4500 for a
# comfortable safety margin to avoid truncation on chunk boundaries.
CHUNK_SIZE = 4500


def translate_to_malayalam(text: str) -> str:
    """Translate *text* from English to Malayalam in safe-sized chunks."""
    if not text.strip():
        return text

    translator = GoogleTranslator(source="en", target="ml")
    paragraphs = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        if current_len + len(para) + 1 > CHUNK_SIZE and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += len(para) + 1

    if current:
        chunks.append("\n".join(current))

    translated_parts: list[str] = []
    for chunk in chunks:
        try:
            result = translator.translate(chunk)
            translated_parts.append(result or chunk)
        except Exception:
            translated_parts.append(chunk)  # fall back to original on error
        time.sleep(0.3)  # gentle rate-limiting

    return "\n".join(translated_parts)


# ── PDF helpers ────────────────────────────────────────────────────────────────
def extract_text_from_pdf(path: str) -> tuple[str, int]:
    """Return (full_text, page_count) from a PDF file."""
    pages_text: list[str] = []
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages_text.append(text)
    return "\n\n".join(pages_text), page_count


def build_translated_pdf(translated_text: str, output_path: str) -> None:
    """Write a nicely-formatted Malayalam PDF to *output_path*."""
    font_ok = register_font()
    font_name = "NotoMalayalam" if font_ok else "Helvetica"

    page_w, page_h = A4
    margin = 20 * mm
    usable_w = page_w - 2 * margin
    font_size = 14
    line_height = font_size * 1.6
    max_chars_per_line = int(usable_w / (font_size * CHAR_WIDTH_RATIO))

    c = canvas.Canvas(output_path, pagesize=A4)
    c.setFont(font_name, font_size)

    y = page_h - margin

    def new_page():
        nonlocal y
        c.showPage()
        c.setFont(font_name, font_size)
        y = page_h - margin

    for paragraph in translated_text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            y -= line_height * 0.5
            if y < margin:
                new_page()
            continue

        # Wrap long lines
        lines = textwrap.wrap(paragraph, width=max_chars_per_line) or [paragraph]
        for line in lines:
            if y < margin + line_height:
                new_page()
            c.drawString(margin, y, line)
            y -= line_height

    c.save()


# ── Background worker ──────────────────────────────────────────────────────────
def _update_job(job_id: str, **kwargs) -> None:
    """Thread-safe helper to update job state."""
    with _jobs_lock:
        jobs[job_id].update(**kwargs)


def process_job(job_id: str, upload_path: str, original_name: str) -> None:
    """Run extraction + translation + PDF generation in a background thread."""
    try:
        # Step 1 – extract
        _update_job(job_id, status="running", progress=10, message="Reading your PDF…")
        text, page_count = extract_text_from_pdf(upload_path)
        if not text.strip():
            _update_job(
                job_id,
                status="error",
                progress=0,
                message="Could not read any text from this PDF. "
                        "It may be a scanned image — please use a text-based PDF.",
            )
            return

        # Step 2 – translate
        _update_job(
            job_id,
            progress=20,
            message=f"Translating {page_count} page(s) to Malayalam… "
                    "This may take a few minutes for long documents.",
        )
        translated = translate_to_malayalam(text)
        _update_job(job_id, progress=80, message="Building the translated PDF…")

        # Step 3 – build PDF
        stem = os.path.splitext(original_name)[0]
        output_name = f"{stem}_malayalam_{job_id[:8]}.pdf"
        output_path = os.path.join(OUTPUT_FOLDER, output_name)
        build_translated_pdf(translated, output_path)

        _update_job(
            job_id,
            status="done",
            progress=100,
            message="Translation complete! Click the button below to download.",
            output_file=output_name,
        )

    except Exception as exc:
        _update_job(job_id, status="error", progress=0, message=f"Something went wrong: {exc}")
    finally:
        # Clean up the uploaded file
        try:
            os.remove(upload_path)
        except OSError:
            pass


# ── Routes ─────────────────────────────────────────────────────────────────────
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/translate", methods=["POST"])
def start_translation():
    if "pdf_file" not in request.files:
        return jsonify(error="No file selected."), 400

    file = request.files["pdf_file"]
    if not file.filename:
        return jsonify(error="No file selected."), 400
    if not allowed_file(file.filename):
        return jsonify(error="Please upload a PDF file."), 400

    # Save upload
    job_id = str(uuid.uuid4())
    safe_name = f"{job_id}.pdf"
    upload_path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(upload_path)

    # Create job record (write under lock)
    with _jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "progress": 5,
            "message": "Starting translation…",
            "output_file": None,
        }

    # Start background thread
    thread = threading.Thread(
        target=process_job,
        args=(job_id, upload_path, file.filename),
        daemon=True,
    )
    thread.start()

    return jsonify(job_id=job_id)


@app.route("/status/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job.get("output_file"):
        return "File not ready.", 404
    output_path = os.path.join(OUTPUT_FOLDER, job["output_file"])
    if not os.path.exists(output_path):
        return "File not found.", 404
    return send_file(output_path, as_attachment=True, download_name=job["output_file"])


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Pre-download the font so first use is instant
    ensure_malayalam_font()
    print("\n✅  Open your web browser and go to:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
