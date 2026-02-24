# English to Malayalam PDF Translator

A simple, user-friendly web app that converts PDF documents from English to Malayalam.
Just double-click one file and open your browser — no technical knowledge needed.

---

## What you need

- A Windows computer
- An internet connection (needed the first time, and for each translation)
- Your PDF file

---

## How to use it — step by step

### First time setup (do this once)

1. **Install Python**
   - Go to <https://www.python.org/downloads/>
   - Click the big yellow **"Download Python"** button
   - Run the installer. **Important:** tick the box that says **"Add Python to PATH"** before clicking Install.

2. **Download this project**
   - Click the green **Code** button on this page, then choose **"Download ZIP"**
   - Unzip the downloaded file to a folder on your Desktop (e.g. `pdf-translator`)

### Every time you want to translate a PDF

3. **Start the app**
   - Open the `pdf-translator` folder
   - Double-click **`start.bat`**
   - A black window will appear — leave it open (it is the engine running the app)
   - Your web browser should open automatically, or you can open it and go to:
     **http://localhost:5000**

4. **Translate your PDF**
   - Click the blue box to choose your PDF file
   - Click the green **"Translate to Malayalam"** button
   - Wait while the translation runs (a progress bar shows how far along it is)
   - For a 10-page document this usually takes 1–2 minutes

5. **Download the result**
   - When it finishes, click the orange **"Download Malayalam PDF"** button
   - The translated PDF will be saved to your Downloads folder

6. **Stop the app**
   - When you are done, close the black window

---

## Privacy note

Your PDF files are **never sent to the internet**. The translation is done through
Google Translate's free service (text only — no file upload), and everything else
runs on your own computer.

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| `python` is not recognised | Re-install Python and tick "Add Python to PATH" |
| Page won't load | Make sure the black window is still open |
| "Could not read any text" error | Your PDF might be a scanned image. Only text-based PDFs are supported |
| Translation looks wrong | Google Translate is used; results are good but not always perfect |
