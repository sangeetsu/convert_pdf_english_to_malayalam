"""
English to Malayalam Document Translator
Supports PDF, DOCX, TXT, HTML, and Markdown input.
Preserves document structure: headings, tables, code blocks, lists.
"""

import os
import re
import uuid
import time
import threading
import requests
from dataclasses import dataclass, field
from flask import Flask, render_template, request, send_file, jsonify
from deep_translator import GoogleTranslator

# PDF extraction
import pdfplumber

# PDF generation — PLATYPUS layout engine
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Preformatted,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# DOCX support
from docx import Document as DocxDocument
from docx.shared import Pt, RGBColor

# HTML support
from bs4 import BeautifulSoup

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
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt", "html", "htm", "md"}

for folder in (UPLOAD_FOLDER, OUTPUT_FOLDER, FONT_DIR):
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

# In-memory job store: job_id -> {status, progress, message, output_file}
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ── Document Block model ───────────────────────────────────────────────────────
@dataclass
class DocBlock:
    """A single structural element of a document."""
    # kind: "h1" | "h2" | "h3" | "para" | "table" | "code" | "list_item" | "spacer"
    kind: str
    text: str = ""
    rows: list = field(default_factory=list)   # for "table": list[list[str]]

    @property
    def is_translatable(self) -> bool:
        return self.kind not in ("code", "spacer") and (
            bool(self.text.strip()) or bool(self.rows)
        )

    @property
    def is_table(self) -> bool:
        return self.kind == "table"


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


# ── Translation Engines ────────────────────────────────────────────────────────
CHUNK_SIZE = 4500
_NUMBERS_ONLY = re.compile(r'^[\d\s.,\-%+()/*=:;]+$')
_URL_PATTERN  = re.compile(r'^https?://\S+$|^www\.\S+$')


def _skip(text: str) -> bool:
    """True when text should not be translated (numbers, URLs, blank)."""
    t = text.strip()
    return not t or bool(_NUMBERS_ONLY.match(t)) or bool(_URL_PATTERN.match(t))


# ── Base engine ────────────────────────────────────────────────────────────────
class TranslateEngine:
    """Abstract base for all translation engines."""
    name        = "base"
    label       = "Base"
    needs_api_key = False
    api_key_env = ""
    description = ""
    setup_hint  = ""

    def translate(self, text: str) -> str:
        raise NotImplementedError

    def translate_large(self, text: str) -> str:
        """Translate text that may exceed single-call limits."""
        if not text.strip() or _skip(text):
            return text
        if len(text) <= CHUNK_SIZE:
            return self.translate(text)
        paras = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for para in paras:
            if current_len + len(para) + 1 > CHUNK_SIZE and current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            current.append(para)
            current_len += len(para) + 1
        if current:
            chunks.append("\n".join(current))
        return "\n".join(self.translate(c) for c in chunks)

    def translate_cell(self, text: str) -> str:
        """Translate a single table cell or short text."""
        if _skip(text):
            return text
        return self.translate(text)

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        return True, ""


# ── 1. Google Translate (free, no key) ─────────────────────────────────────────
class GoogleTranslateEngine(TranslateEngine):
    name        = "google"
    label       = "Google Translate"
    description = "Fast, no setup needed. Basic quality for Indian languages."
    setup_hint  = ""

    def translate(self, text: str) -> str:
        if _skip(text):
            return text
        try:
            result = GoogleTranslator(source="en", target="ml").translate(text)
            time.sleep(0.2)
            return result or text
        except Exception:
            return text


# ── 2. IndicTrans2 (AI4Bharat — local model, best Indic quality) ──────────────
def _patch_transformers_compat() -> None:
    """IndicTransToolkit imports PreTrainedTokenizerBase from
    transformers.tokenization_utils, but transformers>=4.x moved it to
    tokenization_utils_base.  Inject a shim so the old import path works."""
    try:
        import transformers.tokenization_utils as _tu
        if not hasattr(_tu, "PreTrainedTokenizerBase"):
            from transformers.tokenization_utils_base import PreTrainedTokenizerBase
            _tu.PreTrainedTokenizerBase = PreTrainedTokenizerBase
    except Exception:
        pass  # best-effort; the real ImportError will surface naturally


class IndicTrans2Engine(TranslateEngine):
    name        = "indictrans2"
    label       = "IndicTrans2 (AI4Bharat)"
    description = "Best quality for Indian languages. Runs a local AI model."
    setup_hint  = "First run downloads ~800 MB model. Needs: pip install indictranstoolkit torch transformers"

    _model = None
    _tokenizer = None
    _processor = None
    _device = None

    @classmethod
    def _load(cls):
        if cls._model is not None:
            return
        import torch
        _patch_transformers_compat()
        from IndicTransToolkit import IndicProcessor
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        cls._device = "cuda" if torch.cuda.is_available() else "cpu"
        model_name = "ai4bharat/indictrans2-en-indic-dist-200M"
        cls._tokenizer = AutoTokenizer.from_pretrained(
            model_name, trust_remote_code=True
        )
        cls._model = AutoModelForSeq2SeqLM.from_pretrained(
            model_name, trust_remote_code=True
        ).to(cls._device)
        cls._processor = IndicProcessor(inference=True)

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import torch  # noqa: F401
            from transformers import AutoModelForSeq2SeqLM  # noqa: F401
            _patch_transformers_compat()
            from IndicTransToolkit import IndicProcessor  # noqa: F401
            return True, ""
        except ImportError as e:
            return False, (
                f"Missing package: {e.name}. "
                "Run:  pip install indictranstoolkit torch transformers"
            )

    def translate(self, text: str) -> str:
        if _skip(text):
            return text
        try:
            self._load()
            import torch
            batch = self._processor.preprocess_batch(
                [text], src_lang="eng_Latn", tgt_lang="mal_Mlym",
            )
            inputs = self._tokenizer(
                batch, padding="longest", truncation=True,
                max_length=256, return_tensors="pt",
            ).to(self._device)
            with torch.inference_mode():
                outputs = self._model.generate(
                    **inputs, num_beams=5, num_return_sequences=1, max_length=256,
                )
            decoded = self._tokenizer.batch_decode(
                outputs, skip_special_tokens=True,
                clean_up_tokenization_spaces=True,
            )
            result = self._processor.postprocess_batch(decoded, lang="mal_Mlym")
            return result[0] if result else text
        except Exception as e:
            print(f"IndicTrans2 error: {e}")
            return text


# ── 3. Sarvam AI (Indian-language API, free tier) ─────────────────────────────
SECRETS_DIR = os.path.join(BASE_DIR, "secrets")


def _load_secret(filename: str, env_var: str = "") -> str:
    """Return the first non-empty line from secrets/<filename>, or env var."""
    path = os.path.join(SECRETS_DIR, filename)
    if os.path.exists(path):
        with open(path) as fh:
            val = fh.read().strip().splitlines()[0].strip()
        if val:
            return val
    return os.environ.get(env_var, "") if env_var else ""


class SarvamEngine(TranslateEngine):
    name        = "sarvam"
    label       = "Sarvam AI"
    needs_api_key = True
    api_key_env = "SARVAM_API_KEY"
    description = "Best-in-class Indian language API. Free tier available."
    setup_hint  = "Get a free API key at https://www.sarvam.ai"

    def __init__(self, api_key: str = ""):
        self.api_key = (
            api_key
            or _load_secret("sarvam_api_1.txt", "SARVAM_API_KEY")
        )

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            from sarvamai import SarvamAI  # noqa: F401
            return True, ""
        except ImportError:
            return False, "Missing package. Run:  pip install sarvamai"

    def translate(self, text: str) -> str:
        if _skip(text):
            return text
        try:
            from sarvamai import SarvamAI
            client = SarvamAI(api_subscription_key=self.api_key)
            response = client.text.translate(
                input=text[:CHUNK_SIZE],
                source_language_code="en-IN",
                target_language_code="ml-IN",
                speaker_gender="Male",
                mode="formal",
                model="mayura:v1",
                numerals_format="international",
            )
            time.sleep(0.3)
            # SDK returns an object; the translated text is in .translated_text
            return getattr(response, "translated_text", None) or text
        except Exception as e:
            print(f"Sarvam error: {e}")
            return text


# ── 4. Google Gemini (LLM, context-aware, free tier) ──────────────────────────
class GeminiEngine(TranslateEngine):
    name        = "gemini"
    label       = "Google Gemini"
    needs_api_key = True
    api_key_env = "GEMINI_API_KEY"
    description = "Context-aware LLM translation. Free tier: 15 req/min."
    setup_hint  = "Get a free API key at https://aistudio.google.com/apikey"

    _SYSTEM = (
        "You are a professional English to Malayalam translator. "
        "Translate the following English text accurately into Malayalam. "
        "Preserve all technical terms, numbers, proper nouns, and formatting. "
        "Return ONLY the Malayalam translation — no explanations, no preamble."
    )

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            from google import genai  # noqa: F401
            return True, ""
        except ImportError:
            return False, "Missing package. Run:  pip install google-genai"

    def translate(self, text: str) -> str:
        if _skip(text):
            return text
        try:
            client = self._get_client()
            from google.genai import types
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(
                    system_instruction=self._SYSTEM,
                    thinking_config=types.ThinkingConfig(thinking_level="none"),
                ),
                contents=text,
            )
            time.sleep(1.0)   # free tier ≈ 15 RPM → 1 req/s safe
            return response.text.strip() if response.text else text
        except Exception as e:
            print(f"Gemini error: {e}")
            return text


# ── Engine registry ────────────────────────────────────────────────────────────
ENGINES: dict[str, type[TranslateEngine]] = {
    "google":      GoogleTranslateEngine,
    "indictrans2": IndicTrans2Engine,
    "sarvam":      SarvamEngine,
    "gemini":      GeminiEngine,
}


def create_engine(name: str, api_key: str = "") -> TranslateEngine:
    cls = ENGINES.get(name, GoogleTranslateEngine)
    if cls.needs_api_key:
        return cls(api_key=api_key)
    return cls()


def translate_blocks(
    blocks: list[DocBlock],
    engine: TranslateEngine,
    on_progress: "Callable[[int, int], None] | None" = None,
) -> list[DocBlock]:
    """Translate every translatable block, preserving structure.

    on_progress(done, total) is called after each translatable block finishes.
    """
    from typing import Callable  # local import avoids top-level typing dep

    translatable = [b for b in blocks if b.is_translatable]
    total = len(translatable)
    done = 0
    out: list[DocBlock] = []
    for block in blocks:
        if not block.is_translatable:
            out.append(block)
            continue
        if block.is_table:
            new_rows = [
                [engine.translate_cell(cell) if cell.strip() else cell for cell in row]
                for row in block.rows
            ]
            out.append(DocBlock(kind="table", rows=new_rows))
        else:
            out.append(DocBlock(kind=block.kind, text=engine.translate_large(block.text)))
        done += 1
        if on_progress:
            on_progress(done, total)
    return out


# ── PDF Extractor ──────────────────────────────────────────────────────────────
def _body_font_size(page) -> float:
    """Return the most common (body) font size on a pdfplumber page."""
    sizes = [round(ch["size"]) for ch in page.chars if ch.get("size")]
    return float(max(set(sizes), key=sizes.count)) if sizes else 12.0


def _heading_kind(avg_size: float, body: float) -> str:
    if avg_size >= body * 1.5:  return "h1"
    if avg_size >= body * 1.25: return "h2"
    if avg_size >= body * 1.1:  return "h3"
    return "para"


def _is_real_table(raw_rows: list[list[str | None]]) -> bool:
    """Return True only if the table has genuine multi-column data.

    pdfplumber frequently detects background-shading rectangles, underlines,
    and other decorative PDF elements as table borders, producing "phantom"
    tables where all content lives in a single column.  We filter those out.
    """
    if not raw_rows:
        return False
    cols = len(raw_rows[0])
    if cols < 2:
        return False
    # Count rows where at least 2 cells have real content
    multi_col_rows = 0
    total_cells = 0
    filled_cells = 0
    for row in raw_rows:
        filled = sum(1 for c in row if c and c.strip())
        if filled >= 2:
            multi_col_rows += 1
        total_cells += len(row)
        filled_cells += filled
    # Need at least 2 multi-column rows (header + 1 data row)
    if multi_col_rows < 2:
        return False
    # Need at least 40% of cells to be filled (filters sparse grids)
    fill_ratio = filled_cells / total_cells if total_cells else 0
    return fill_ratio >= 0.40


# Regex for common header/footer patterns to strip
_HEADER_RE = re.compile(
    r'^\s*Notes on Disease.*Training\s*$|'
    r'^\s*Livestock Management Training Centre.*$|'
    r'^\s*\d+\s*$',   # bare page numbers
    re.IGNORECASE,
)


def _extract_page_blocks(page, body: float) -> list[DocBlock]:
    """Extract structured blocks from a single pdfplumber page."""
    blocks: list[DocBlock] = []

    # ── Real tables ──────────────────────────────────────────────────────
    table_bboxes: list[tuple] = []
    for tf in page.find_tables():
        raw = tf.extract() or []
        if _is_real_table(raw):
            table_bboxes.append(tf.bbox)
            rows = [[cell or "" for cell in row] for row in raw]
            blocks.append(DocBlock(kind="table", rows=rows))

    def _in_table(w) -> bool:
        for x0, top, x1, bot in table_bboxes:
            if x0 <= w["x0"] and w["x1"] <= x1 and top <= w["top"] and w["bottom"] <= bot:
                return True
        return False

    # ── Words outside tables ─────────────────────────────────────────────
    page_height = float(page.height)
    header_cutoff = 45     # ignore top 45pt (headers)
    footer_cutoff = page_height - 45   # ignore bottom 45pt (footers / page nums)

    words = page.extract_words(
        extra_attrs=["fontname", "size"], x_tolerance=3, y_tolerance=3
    )
    words = [
        w for w in words
        if not _in_table(w)
        and w["top"] > header_cutoff
        and w["top"] < footer_cutoff
    ]
    if not words:
        return blocks

    # Group into lines by top-coordinate (tolerance 2pt for baseline jitter)
    line_map: dict[int, list] = {}
    for w in words:
        key = round(w["top"])
        # Snap to nearby existing key within 2pt
        for existing in line_map:
            if abs(existing - key) <= 2:
                key = existing
                break
        line_map.setdefault(key, []).append(w)

    sorted_tops = sorted(line_map.keys())
    prev_bottom: float = 0
    para_lines: list[str] = []
    para_kind = "para"

    def _flush() -> None:
        nonlocal para_lines, para_kind
        if para_lines:
            text = " ".join(para_lines)
            # Skip headers/footers
            if not _HEADER_RE.match(text):
                blocks.append(DocBlock(kind=para_kind, text=text))
            para_lines, para_kind = [], "para"

    for idx, top in enumerate(sorted_tops):
        line_words = sorted(line_map[top], key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in line_words)
        sizes = [w.get("size", body) for w in line_words]
        avg = sum(sizes) / len(sizes) if sizes else body
        kind = _heading_kind(avg, body)

        # Detect paragraph break: large vertical gap OR heading change
        line_top = min(w["top"] for w in line_words)
        gap = line_top - prev_bottom if prev_bottom else 0
        body_leading = body * 1.4   # expected line spacing
        is_para_break = gap > body_leading * 1.3  # >30% more than normal leading

        if is_para_break and para_lines:
            _flush()
            blocks.append(DocBlock(kind="spacer"))
        elif kind != para_kind and para_lines:
            _flush()

        # Detect numbered/lettered list items
        if re.match(r'^\d+[a-z]?[\)\.]\s', line_text) or re.match(r'^[a-z][\)\.]\s', line_text):
            if para_lines:
                _flush()
            kind = "list_item" if kind == "para" else kind

        para_kind = kind
        para_lines.append(line_text)
        prev_bottom = max(w["bottom"] for w in line_words)

    _flush()
    return blocks


def extract_pdf(path: str, page_only: int | None = None) -> tuple[list[DocBlock], int]:
    """Extract structured blocks from a text-based PDF.

    If *page_only* is given (1-based), only that page is extracted (for preview).
    """
    blocks: list[DocBlock] = []
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        pages = [pdf.pages[page_only - 1]] if page_only else pdf.pages
        # Compute body font size from first few pages for consistency
        sample_pages = pdf.pages[:min(5, page_count)]
        all_sizes: list[int] = []
        for sp in sample_pages:
            all_sizes.extend(round(ch["size"]) for ch in sp.chars if ch.get("size"))
        body = float(max(set(all_sizes), key=all_sizes.count)) if all_sizes else 12.0

        for page in pages:
            page_blocks = _extract_page_blocks(page, body)
            blocks.extend(page_blocks)
            blocks.append(DocBlock(kind="spacer"))

    return blocks, page_count


# ── DOCX Extractor ─────────────────────────────────────────────────────────────
def extract_docx(path: str) -> tuple[list[DocBlock], int]:
    """Extract structured blocks from a Word document."""
    from docx.text.paragraph import Paragraph as _Para
    from docx.table import Table as _Table

    doc = DocxDocument(path)
    blocks: list[DocBlock] = []

    for child in doc.element.body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            para = _Para(child, doc)
            text = para.text.strip()
            if not text:
                blocks.append(DocBlock(kind="spacer"))
                continue
            style = (para.style.name or "").lower()
            if   "heading 1" in style:             kind = "h1"
            elif "heading 2" in style:             kind = "h2"
            elif "heading" in style:               kind = "h3"
            elif "code" in style or "preformat" in style: kind = "code"
            elif "list" in style or "bullet" in style:    kind = "list_item"
            else:                                          kind = "para"
            blocks.append(DocBlock(kind=kind, text=text))
        elif tag == "tbl":
            from docx.table import Table as _Tbl
            tbl = _Tbl(child, doc)
            rows = [[c.text.strip() for c in row.cells] for row in tbl.rows]
            blocks.append(DocBlock(kind="table", rows=rows))

    return blocks, 1


# ── TXT Extractor ──────────────────────────────────────────────────────────────
def extract_txt(path: str) -> tuple[list[DocBlock], int]:
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    blocks: list[DocBlock] = []
    for para in content.split("\n\n"):
        para = para.strip()
        if para:
            blocks.append(DocBlock(kind="para", text=para))
            blocks.append(DocBlock(kind="spacer"))
    return blocks, 1


# ── HTML Extractor ─────────────────────────────────────────────────────────────
def extract_html(path: str) -> tuple[list[DocBlock], int]:
    with open(path, encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    blocks: list[DocBlock] = []
    root = soup.body or soup

    def _process(elem) -> None:
        name = getattr(elem, "name", None)
        if name is None:
            return
        if name == "h1":
            blocks.append(DocBlock(kind="h1", text=elem.get_text(strip=True)))
        elif name == "h2":
            blocks.append(DocBlock(kind="h2", text=elem.get_text(strip=True)))
        elif name in ("h3", "h4", "h5", "h6"):
            blocks.append(DocBlock(kind="h3", text=elem.get_text(strip=True)))
        elif name == "p":
            t = elem.get_text(separator=" ", strip=True)
            if t:
                blocks.append(DocBlock(kind="para", text=t))
        elif name in ("pre", "code"):
            blocks.append(DocBlock(kind="code", text=elem.get_text()))
        elif name in ("ul", "ol"):
            for li in elem.find_all("li", recursive=False):
                blocks.append(DocBlock(kind="list_item", text=li.get_text(strip=True)))
        elif name == "table":
            rows = []
            for tr in elem.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append(DocBlock(kind="table", rows=rows))
        elif name in ("div", "article", "section", "main", "body"):
            for child in elem.children:
                _process(child)

    for child in root.children:
        _process(child)
    return blocks, 1


# ── Markdown Extractor ─────────────────────────────────────────────────────────
def extract_md(path: str) -> tuple[list[DocBlock], int]:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    blocks: list[DocBlock] = []
    in_code = False
    code_lines: list[str] = []
    para_lines: list[str] = []

    def flush_para() -> None:
        if para_lines:
            t = " ".join(l.strip() for l in para_lines if l.strip())
            if t:
                blocks.append(DocBlock(kind="para", text=t))
            para_lines.clear()

    for raw in lines:
        raw = raw.rstrip("\n")
        if raw.startswith("```"):
            if in_code:
                blocks.append(DocBlock(kind="code", text="\n".join(code_lines)))
                code_lines.clear()
                in_code = False
            else:
                flush_para()
                in_code = True
            continue
        if in_code:
            code_lines.append(raw)
            continue
        m = re.match(r'^(#{1,3})\s+(.*)', raw)
        if m:
            flush_para()
            blocks.append(DocBlock(kind=f"h{len(m.group(1))}", text=m.group(2).strip()))
            continue
        m = re.match(r'^[\-\*\+]\s+(.*)', raw) or re.match(r'^\d+\.\s+(.*)', raw)
        if m:
            flush_para()
            blocks.append(DocBlock(kind="list_item", text=m.group(1).strip()))
            continue
        if not raw.strip():
            flush_para()
            blocks.append(DocBlock(kind="spacer"))
            continue
        # Strip inline markdown
        t = re.sub(r'\*\*(.+?)\*\*', r'\1', raw)
        t = re.sub(r'\*(.+?)\*',     r'\1', t)
        t = re.sub(r'`(.+?)`',       r'\1', t)
        t = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', t)
        para_lines.append(t)

    if in_code and code_lines:
        blocks.append(DocBlock(kind="code", text="\n".join(code_lines)))
    flush_para()
    return blocks, 1


# ── Format dispatcher ──────────────────────────────────────────────────────────
def extract_document(
    path: str, ext: str, *, page_only: int | None = None,
) -> tuple[list[DocBlock], int]:
    ext = ext.lower().lstrip(".")
    if ext == "pdf":               return extract_pdf(path, page_only=page_only)
    if ext == "docx":              return extract_docx(path)
    if ext in ("html", "htm"):     return extract_html(path)
    if ext == "md":                return extract_md(path)
    return extract_txt(path)


# ── PDF Renderer (PLATYPUS) ────────────────────────────────────────────────────
def _safe_para(text: str, style: ParagraphStyle) -> Paragraph:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        return Paragraph(safe, style)
    except Exception:
        return Paragraph(safe.encode("ascii", "replace").decode(), style)


def build_translated_pdf(blocks: list[DocBlock], output_path: str) -> None:
    """Render structured blocks to a well-formatted Malayalam PDF."""
    font_ok = register_font()
    fn = "NotoMalayalam" if font_ok else "Helvetica"

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm, topMargin=25*mm, bottomMargin=20*mm,
    )

    body = ParagraphStyle("Mal_body", fontName=fn, fontSize=13, leading=21,
                          spaceAfter=8, wordWrap="CJK")
    h1   = ParagraphStyle("Mal_h1", fontName=fn, fontSize=22, leading=30,
                          spaceAfter=12, spaceBefore=18,
                          textColor=colors.HexColor("#1e3a5f"), wordWrap="CJK")
    h2   = ParagraphStyle("Mal_h2", fontName=fn, fontSize=17, leading=24,
                          spaceAfter=10, spaceBefore=14,
                          textColor=colors.HexColor("#2d6a9f"), wordWrap="CJK")
    h3   = ParagraphStyle("Mal_h3", fontName=fn, fontSize=14, leading=21,
                          spaceAfter=8, spaceBefore=10,
                          textColor=colors.HexColor("#3d7ab0"), wordWrap="CJK")
    code = ParagraphStyle("Code", fontName="Courier", fontSize=10, leading=14,
                          leftIndent=10, backColor=colors.HexColor("#f5f5f5"),
                          spaceAfter=6, spaceBefore=6)
    lst  = ParagraphStyle("Mal_list", fontName=fn, fontSize=13, leading=20,
                          spaceAfter=4, leftIndent=20, wordWrap="CJK")

    smap = {"h1": h1, "h2": h2, "h3": h3, "para": body, "list_item": lst}
    story = []

    for block in blocks:
        if block.kind == "spacer":
            story.append(Spacer(1, 6))
            continue

        if block.is_table and block.rows:
            cols = max(len(r) for r in block.rows)
            # Smaller dedicated style so cell content wraps safely
            cell_style = ParagraphStyle(
                "Mal_cell", fontName=fn, fontSize=10, leading=15, wordWrap="CJK"
            )
            # Cap each cell at 1000 chars — ReportLab cannot page-break
            # *inside* a single cell, so an oversized cell crashes the build.
            MAX_CELL = 1000
            def _cell(text: str) -> Paragraph:
                if len(text) > MAX_CELL:
                    text = text[:MAX_CELL] + "\u2026"
                return _safe_para(text, cell_style)

            data = [
                [_cell(cell) for cell in (row + [""] * (cols - len(row)))]
                for row in block.rows
            ]
            cw = (A4[0] - 40 * mm) / cols
            # splitByRow lets ReportLab break the table between rows;
            # repeatRows=1 re-prints the header on every new page.
            t = Table(data, colWidths=[cw] * cols, repeatRows=1, splitByRow=True)
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2d6a9f")),
                ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
                ("FONTNAME",      (0, 0), (-1, 0),  fn),
                ("FONTNAME",      (0, 1), (-1, -1), fn),
                ("FONTSIZE",      (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f8")]),
                ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#aaaaaa")),
                ("ALIGN",         (0, 0), (-1, -1), "LEFT"),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",    (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
            continue

        if block.kind == "code":
            story.append(Preformatted(block.text, code))
        else:
            prefix = "• " if block.kind == "list_item" else ""
            story.append(_safe_para(prefix + block.text, smap.get(block.kind, body)))

    if not story:
        story.append(_safe_para("(No content)", body))
    doc.build(story)


# ── DOCX Renderer ──────────────────────────────────────────────────────────────
def build_translated_docx(blocks: list[DocBlock], output_path: str) -> None:
    """Render structured blocks to a Word document."""
    doc = DocxDocument()
    doc.styles["Normal"].font.name = "Noto Sans Malayalam"
    doc.styles["Normal"].font.size = Pt(13)

    heading_colors = {
        "h1": RGBColor(0x1e, 0x3a, 0x5f),
        "h2": RGBColor(0x2d, 0x6a, 0x9f),
        "h3": RGBColor(0x3d, 0x7a, 0xb0),
    }

    for block in blocks:
        if block.kind == "spacer":
            doc.add_paragraph("")
            continue

        if block.is_table and block.rows:
            cols = max(len(r) for r in block.rows)
            tbl = doc.add_table(rows=len(block.rows), cols=cols)
            tbl.style = "Table Grid"
            for i, row in enumerate(block.rows):
                for j, cell in enumerate(row):
                    if j < cols:
                        tbl.rows[i].cells[j].text = cell
                        if i == 0:
                            for run in tbl.rows[i].cells[j].paragraphs[0].runs:
                                run.bold = True
            doc.add_paragraph("")
            continue

        if block.kind.startswith("h"):
            level = int(block.kind[1])
            h = doc.add_heading(block.text, level=level)
            c = heading_colors.get(block.kind)
            if c:
                for run in h.runs:
                    run.font.color.rgb = c
        elif block.kind == "code":
            p = doc.add_paragraph(block.text)
            for run in p.runs:
                run.font.name = "Courier New"
                run.font.size = Pt(10)
        elif block.kind == "list_item":
            doc.add_paragraph(block.text, style="List Bullet")
        else:
            doc.add_paragraph(block.text)

    doc.save(output_path)


# ── Background worker ──────────────────────────────────────────────────────────
def _update_job(job_id: str, **kwargs) -> None:
    """Thread-safe helper to update job state."""
    with _jobs_lock:
        jobs[job_id].update(**kwargs)


def process_job(
    job_id: str, upload_path: str, original_name: str, output_format: str,
    preview: bool = False, engine_name: str = "google", api_key: str = "",
) -> None:
    """Extract → translate → render in a background thread.

    If *preview* is True, only page 1 is extracted and translated.
    """
    try:
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "txt"
        engine = create_engine(engine_name, api_key)

        # Step 1 – extract structure
        page_label = "first page (preview)" if preview else "document"
        _update_job(job_id, status="running", progress=10,
                    message=f"Reading your {page_label}…")
        blocks, page_count = extract_document(
            upload_path, ext, page_only=1 if preview else None,
        )

        content_blocks = [b for b in blocks if b.kind != "spacer"]
        if not content_blocks:
            _update_job(
                job_id, status="error", progress=0,
                message="Could not read any text from this document. "
                        "PDFs must be text-based (not scanned images).",
            )
            return

        headings = sum(1 for b in content_blocks if b.kind.startswith("h"))
        tables   = sum(1 for b in content_blocks if b.is_table)
        total_translatable = sum(1 for b in blocks if b.is_translatable)

        # Step 2 – translate with live per-block progress updates
        # Progress range 15 → 85 % is reserved for translation.
        TRANS_START, TRANS_END = 15, 85

        def _on_progress(done: int, total: int) -> None:
            frac = done / total if total else 1
            pct  = int(TRANS_START + frac * (TRANS_END - TRANS_START))
            kind_counts = f"{headings} heading(s), {tables} table(s)"
            _update_job(
                job_id, progress=pct,
                trans_done=done, trans_total=total,
                message=(
                    f"Translating section {done} of {total} "
                    f"[{kind_counts}] — {pct}% done"
                ),
            )

        _update_job(
            job_id, progress=TRANS_START,
            message=(
                f"Starting translation of {page_count} page(s) — "
                f"{total_translatable} section(s) to translate "
                f"({headings} heading(s), {tables} table(s))"
            ),
        )
        translated = translate_blocks(blocks, engine=engine, on_progress=_on_progress)

        # Step 3 – render
        _update_job(job_id, progress=80, message="Building the translated document…")
        stem = os.path.splitext(original_name)[0]
        if output_format == "docx":
            out_name = f"{stem}_malayalam_{job_id[:8]}.docx"
            build_translated_docx(translated, os.path.join(OUTPUT_FOLDER, out_name))
        else:
            out_name = f"{stem}_malayalam_{job_id[:8]}.pdf"
            build_translated_pdf(translated, os.path.join(OUTPUT_FOLDER, out_name))

        _update_job(
            job_id, status="done", progress=100,
            message="Translation complete! Click the button below to download.",
            output_file=out_name,
            output_format=output_format,
        )

    except Exception as exc:
        _update_job(job_id, status="error", progress=0,
                    message=f"Something went wrong: {exc}")
    finally:
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
        exts = ", ".join(sorted(ALLOWED_EXTENSIONS)).upper()
        return jsonify(error=f"Unsupported file type. Accepted formats: {exts}"), 400

    output_format = request.form.get("output_format", "pdf").lower()
    if output_format not in ("pdf", "docx"):
        output_format = "pdf"
    preview     = request.form.get("preview", "0") == "1"
    engine_name = request.form.get("engine", "google")
    api_key     = request.form.get("api_key", "")

    # Quick check: if engine needs API key, validate it's available from any source
    # (form input, env var, OR secrets/ file — let the engine resolve it)
    engine_cls = ENGINES.get(engine_name, GoogleTranslateEngine)
    if engine_cls.needs_api_key:
        resolved_key = api_key or os.environ.get(engine_cls.api_key_env, "")
        if not resolved_key:
            # Try instantiating the engine so it can load from secrets/
            try:
                resolved_key = engine_cls(api_key="").api_key
            except Exception:
                resolved_key = ""
        if not resolved_key:
            return jsonify(error=f"{engine_cls.label} requires an API key."), 400

    job_id  = str(uuid.uuid4())
    ext     = file.filename.rsplit(".", 1)[-1].lower()
    upload_path = os.path.join(UPLOAD_FOLDER, f"{job_id}.{ext}")
    file.save(upload_path)

    with _jobs_lock:
        jobs[job_id] = {
            "status": "running", "progress": 5,
            "message": "Starting translation…", "output_file": None,
            "start_time": time.time(),
            "trans_done": 0, "trans_total": 0,
        }

    threading.Thread(
        target=process_job,
        args=(job_id, upload_path, file.filename, output_format, preview,
              engine_name, api_key),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


@app.route("/engines")
def list_engines():
    """Return available translation engines and their status."""
    result = []
    for name, cls in ENGINES.items():
        available, msg = cls.check_available()
        result.append({
            "name":          name,
            "label":         cls.label,
            "description":   cls.description,
            "needs_api_key": cls.needs_api_key,
            "api_key_env":   cls.api_key_env,
            "setup_hint":    cls.setup_hint,
            "available":     available,
            "error":         msg,
        })
    return jsonify(result)


@app.route("/status/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    resp = dict(job)
    # Add elapsed time for the frontend ETA calculation
    if "start_time" in resp:
        resp["elapsed_sec"] = round(time.time() - resp.pop("start_time"), 1)
    else:
        resp.pop("start_time", None)
    return jsonify(resp)


@app.route("/download/<job_id>")
def download(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job.get("output_file"):
        return "File not ready.", 404
    out_path = os.path.join(OUTPUT_FOLDER, job["output_file"])
    if not os.path.exists(out_path):
        return "File not found.", 404
    return send_file(out_path, as_attachment=True, download_name=job["output_file"])


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_malayalam_font()
    print("\n✅  Open your web browser and go to:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
