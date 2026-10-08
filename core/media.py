"""
Images and dropped-in files: screenshots, image analysis/enhancement and the
jarvis_input/ folder.
"""

import base64
import os
import re
import shutil
import subprocess
import threading
import time

from PIL import Image, ImageEnhance, ImageGrab

from . import brain, config, state
from .tts import speak
from .web import fetch_text

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CODE_EXTS  = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
              ".h", ".rs", ".go", ".rb", ".php", ".sql"}
TEXT_EXTS  = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
              ".toml", ".ini", ".cfg", ".sh", ".bat", ".html", ".css"}
_ENHANCE_WORDS = ["enhance", "improve", "fix", "sharpen", "brighten",
                  "denoise", "clean up", "make better", "increase contrast", "upscale"]


def guess_file_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in CODE_EXTS:
        return "code"
    if ext in TEXT_EXTS:
        return "text"
    return "file"


# ── Images ────────────────────────────────────────────────────────────────────
def image_to_base64(path):
    """Read an image file and return (base64_str, media_type)."""
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}
    media_type = media_map.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type


def analyze_image(image_path, prompt):
    """
    Ask Claude about an image. Only the text answer goes into chat history, so the
    base64 image isn't re-sent on every later call.
    """
    b64, media_type = image_to_base64(image_path)
    try:
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=200,
            system="You are a voice assistant. Answer only what was asked about the image in 1-3 concise sentences. No markdown, no bullet points, no headers — plain spoken sentences only.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt or "Describe this image concisely."},
            ]}],
        )
        result = response.content[0].text.strip()
        brain.remember(f"[Image analysis request] {prompt}", f"[Image analysis result] {result}")
        return result
    except Exception as e:
        return f"Image analysis failed: {e}"


def take_screenshot(prompt):
    """Capture the full screen, save it to jarvis_output/, and analyze it."""
    os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
    path = os.path.join(config.JARVIS_OUTPUT_DIR, f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
    try:
        ImageGrab.grab().save(path)
        print(f"  Screenshot saved: {path}")
    except Exception as e:
        return f"Screenshot failed: {e}"
    return analyze_image(path, prompt or "Describe what's on screen.")


def _pillow_enhance(image_path, prompt):
    """Pillow-based enhancement fallback when Real-ESRGAN isn't available."""
    import json
    try:
        b64, media_type = image_to_base64(image_path)
        response = config.client.messages.create(
            model=config.MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": (
                    f"User wants: '{prompt}'. Return ONLY JSON: "
                    '{"brightness":1.0,"contrast":1.0,"sharpness":1.0,"color":1.0,"description":"..."} '
                    "1.0=no change, range 0.5-2.0 (sharpness up to 3.0)."
                )},
            ]}],
        )
        raw = response.content[0].text.strip()
        params = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        img = Image.open(image_path).convert("RGB")
        img = ImageEnhance.Brightness(img).enhance(params.get("brightness", 1.0))
        img = ImageEnhance.Contrast(img).enhance(params.get("contrast", 1.0))
        img = ImageEnhance.Sharpness(img).enhance(params.get("sharpness", 1.0))
        img = ImageEnhance.Color(img).enhance(params.get("color", 1.0))
        os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(config.JARVIS_OUTPUT_DIR, f"enhanced_{time.strftime('%Y%m%d_%H%M%S')}.png")
        img.save(out_path)
        os.startfile(out_path)
        return params.get("description", "Enhancement applied.")
    except Exception as e:
        return f"Pillow enhancement failed: {e}"


def enhance_image(image_path, prompt):
    """Run Real-ESRGAN upscaling (or the Pillow fallback) and return a result string."""
    if not os.path.exists(config.ESRGAN_EXE):
        result = _pillow_enhance(image_path, prompt)
    else:
        os.makedirs(config.JARVIS_OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(config.JARVIS_OUTPUT_DIR, f"upscaled_{time.strftime('%Y%m%d_%H%M%S')}.png")
        speak("Upscaling image, this may take a moment.")
        try:
            subprocess.run(
                [config.ESRGAN_EXE, "-i", image_path, "-o", out_path, "-s", "4", "-n", "realesrgan-x4plus"],
                check=True, timeout=120,
            )
            os.startfile(out_path)
            result = "Image upscaled 4x and saved to jarvis_output."
        except subprocess.TimeoutExpired:
            result = "Upscaling timed out. Try a smaller image."
        except subprocess.CalledProcessError as e:
            result = f"Upscaling failed: {e}"
        except Exception as e:
            result = f"Upscaling error: {e}"
    brain.remember(f"[Image enhancement request] {prompt}", f"[Enhancement result] {result}")
    return result


# ── Input folder ──────────────────────────────────────────────────────────────
def read_pdf(path):
    """Extract text from the first 10 pages of a PDF (pypdf, or PyPDF2 as a fallback)."""
    try:
        import pypdf as pdf_lib
    except ImportError:
        try:
            import PyPDF2 as pdf_lib
        except ImportError:
            return "(could not read PDF: install pypdf)"
    try:
        with open(path, "rb") as f:
            reader = pdf_lib.PdfReader(f)
            return "\n".join(p.extract_text() or "" for p in reader.pages[:10])[:4000]
    except Exception as e:
        return f"(could not read PDF: {e})"


def archive_input_file(src_path):
    """Move a processed input file into this session's archive folder."""
    if not state.session_archive:
        try:
            os.remove(src_path)
        except Exception:
            pass
        return
    os.makedirs(state.session_archive, exist_ok=True)
    filename = os.path.basename(src_path)
    dest = os.path.join(state.session_archive, filename)
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        dest = os.path.join(state.session_archive, f"{base}_{int(time.time())}{ext}")
    try:
        shutil.move(src_path, dest)
    except Exception as e:
        print(f"  Could not archive {filename}: {e}")


def _read_text(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def process_input_folder(prompt):
    """
    Read every file in jarvis_input/, answer the prompt about them, archive them,
    and return {"speech": <spoken answer>, "context": <combined text for chained actions>}.
    Image enhancement runs in background threads.
    """
    os.makedirs(config.JARVIS_INPUT_DIR, exist_ok=True)
    all_items = [os.path.join(config.JARVIS_INPUT_DIR, fn) for fn in os.listdir(config.JARVIS_INPUT_DIR)
                 if os.path.isfile(os.path.join(config.JARVIS_INPUT_DIR, fn))]
    if not all_items:
        return {"speech": "No files found in the input folder.", "context": ""}

    wants_enhancement = any(kw in (prompt or "").lower() for kw in _ENHANCE_WORDS)
    context_parts, archived, bg_threads = [], [], []

    for fpath in all_items:
        fname = os.path.basename(fpath)
        ext   = os.path.splitext(fname)[1].lower()
        try:
            if ext in IMAGE_EXTS:
                if wants_enhancement:
                    def _bg(p=fpath, pr=prompt):
                        speak(enhance_image(p, pr))
                        archive_input_file(p)
                    bg_threads.append(threading.Thread(target=_bg, daemon=True))
                    context_parts.append(f"[Image: {fname}] (enhancement queued in background)")
                else:
                    context_parts.append(f"[Image: {fname}] {analyze_image(fpath, prompt or 'Describe this image.')}")
                    archived.append(fpath)
            elif ext == ".txt":
                raw  = _read_text(fpath).strip()
                urls = re.findall(r"https?://\S+", raw)
                if urls and len(raw.split()) <= 15:   # a short note that's really just links
                    for url in urls[:3]:
                        try:
                            context_parts.append(f"[URL: {url}]\n{fetch_text(url)}")
                        except Exception as e:
                            context_parts.append(f"[URL: {url}] (fetch failed: {e})")
                else:
                    context_parts.append(f"[File: {fname}]\n{raw[:4000]}")
                archived.append(fpath)
            elif ext in TEXT_EXTS or ext in CODE_EXTS:
                context_parts.append(f"[File: {fname}]\n{_read_text(fpath)[:4000]}")
                archived.append(fpath)
            elif ext == ".pdf":
                context_parts.append(f"[PDF: {fname}]\n{read_pdf(fpath)}")
                archived.append(fpath)
            else:
                context_parts.append(f"[File: {fname}] (unsupported type — skipped)")
        except Exception as e:
            context_parts.append(f"[File: {fname}] (read error: {e})")

    for t in bg_threads:
        t.start()
    for fpath in archived:
        archive_input_file(fpath)

    all_context = "\n\n".join(context_parts)
    if not all_context.strip():
        return {"speech": "Could not read any content from the input folder.", "context": ""}

    summary_prompt = prompt or "Briefly describe what's in these files in 2-3 spoken sentences."
    try:
        resp = config.client.messages.create(
            model=config.MODEL,
            max_tokens=250,
            system="You are a voice assistant. Answer in 1-3 concise spoken sentences. No markdown, no lists.",
            messages=[{"role": "user", "content": f"Context:\n{all_context[:5000]}\n\nRequest: {summary_prompt}"}],
        )
        speech = resp.content[0].text.strip()
    except Exception:
        speech = f"Processed {len(archived)} item(s) from the input folder."
    brain.remember(f"[Input folder contents]\n{all_context}", speech)

    if bg_threads:
        speech += " Enhancement is running in the background."
    return {"speech": speech, "context": all_context}
