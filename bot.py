#!/usr/bin/env python3
"""
Fixed Text Leech Bot (Koyeb + Termux ready)
- Handles any URL inside text files (regex-based)
- Adds safe reply helper (no crash if message missing)
- UTF-8 and fallback decoding for non-English files
- Includes port 8000 HTTP health server for Koyeb
"""

import os
import re
import tempfile
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
import requests
from yt_dlp import YoutubeDL

# === CONFIG ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Set this in Koyeb environment variables
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing!")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
logging.basicConfig(level=logging.INFO)
users_state = {}  # in-memory session tracking


# --- Helpers ---
def safe_send(message, text):
    """Reply safely without crashing."""
    try:
        bot.reply_to(message, text)
    except Exception:
        try:
            bot.send_message(message.chat.id, text)
        except Exception:
            logging.exception("Failed to send message safely.")


def is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith('.pdf') or 'application/pdf' in lower


def parse_urls_from_text(text: str):
    """Detect all URLs in text (anywhere in lines)."""
    pattern = r'https?://[^\s<>"\']+'
    urls = re.findall(pattern, text)
    return urls


# --- Commands ---
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    safe_send(
        message,
        "📘 *Text Leech Bot Ready!*\n\n"
        "Send /pw to start — then upload a text file containing video or PDF URLs.\n"
        "Each line can have any text, I’ll automatically detect links.",
    )


@bot.message_handler(commands=['pw'])
def cmd_pw(message):
    uid = message.from_user.id
    users_state[uid] = {"stage": "await_file"}
    safe_send(message, "📄 Please send your text file (.txt). I’ll extract all video and PDF URLs from it.")


# --- File Handler ---
@bot.message_handler(content_types=['document'])
def handle_document(message):
    uid = message.from_user.id
    state = users_state.get(uid, {})
    if state.get("stage") != "await_file":
        safe_send(message, "I wasn't expecting a file. Send /pw to start.")
        return

    doc = message.document
    safe_send(message, "📥 File received. Processing...")

    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)

    # Decode safely with UTF-8 fallback
    try:
        text = downloaded.decode("utf-8")
    except Exception:
        text = downloaded.decode("latin1", errors="ignore")

    urls = parse_urls_from_text(text)
    if not urls:
        safe_send(message, "⚠️ No valid URLs found! Ensure each link starts with http(s).")
        users_state.pop(uid, None)
        return

    pdfs = [u for u in urls if is_pdf_url(u)]
    videos = [u for u in urls if not is_pdf_url(u)]
    state.update({"stage": "choosing_link", "urls": urls, "pdfs": pdfs, "videos": videos})

    summary = f"✅ Found {len(urls)} URLs — {len(videos)} videos, {len(pdfs)} PDFs.\n\n"
    for i, u in enumerate(urls, start=1):
        t = "PDF" if is_pdf_url(u) else "VIDEO"
        summary += f"{i}. [{t}] {u[:70]}...\n"
    summary += "\n💡 Reply with the number of the link you want to download (e.g. 1)."
    safe_send(message, summary)


# --- Text flow ---
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    state = users_state.get(uid)
    if not state:
        safe_send(message, "Send /pw to start.")
        return

    stage = state.get("stage")
    if stage == "choosing_link":
        try:
            idx = int(text)
        except:
            safe_send(message, "⚠️ Please send a valid number from the list.")
            return

        urls = state["urls"]
        if idx < 1 or idx > len(urls):
            safe_send(message, "Index out of range. Try again.")
            return

        chosen_url = urls[idx - 1]
        state.update({"chosen_index": idx, "chosen_url": chosen_url, "stage": "ask_batch"})
        safe_send(message, f"Selected URL #{idx}:\n{chosen_url}\n\nNow send a *batch/name* for this download.")
        return

    if stage == "ask_batch":
        batch = text.replace('/', '_')[:64]
        state.update({"batch": batch, "stage": "ask_quality"})
        safe_send(message, "Select quality — reply with *480* or *720*.")
        return

    if stage == "ask_quality":
        if text not in ('480', '720'):
            safe_send(message, "Please reply with exactly *480* or *720*.")
            return
        state.update({"quality": text, "stage": "ask_token"})
        safe_send(message, "Send your *pw token* (used for API). Keep it private.")
        return

    if stage == "ask_token":
        token = text.strip()
        state.update({"token": token, "stage": "downloading"})
        safe_send(message, "⏳ Download started... please wait, I’ll upload the file once ready.")
        try:
            file_path = handle_download_and_prepare(uid, state)
        except Exception as e:
            logging.exception("Download error")
            safe_send(message, f"❌ Download failed: {e}")
            users_state.pop(uid, None)
            return

        try:
            with open(file_path, "rb") as f:
                bot.send_document(uid, f, caption=f"📦 Batch: {state.get('batch')} | Index: {state.get('chosen_index')}")
        except Exception as e:
            logging.exception("Upload error")
            safe_send(message, f"Upload failed: {e}")
        finally:
            try:
                os.remove(file_path)
            except:
                pass
            users_state.pop(uid, None)
        return

    safe_send(message, "Follow the flow: /pw → send file → choose index → set batch → quality → token.")


# --- Download logic ---
def handle_download_and_prepare(uid: int, state: dict) -> str:
    chosen_url = state["chosen_url"]
    token = state["token"]
    quality = state["quality"]
    batch = state["batch"]

    api_base = "https://anonymouspwplayer-25261acd1521.herokuapp.com/pw"
    final_url = f"{api_base}?url={chosen_url}&token={token}"
    logging.info("Final URL: %s", final_url)

    tmpdir = tempfile.mkdtemp(prefix="tlb_")

    if is_pdf_url(chosen_url):
        local_name = f"{batch}_{state['chosen_index']}.pdf"
        out_path = os.path.join(tmpdir, local_name)
        with requests.get(chosen_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, 'wb') as fw:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        fw.write(chunk)
        return out_path

    ydl_opts = {
        'outtmpl': os.path.join(tmpdir, f"{batch}_%(id)s.%(ext)s"),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
    }
    ydl_opts['format'] = "bestvideo[height<=720]+bestaudio/best" if quality == '720' else "bestvideo[height<=480]+bestaudio/best"

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(final_url, download=True)
        if isinstance(info, dict) and 'requested_downloads' in info:
            downloads = info['requested_downloads']
            if downloads and downloads[0].get('filepath'):
                return downloads[0]['filepath']
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if files:
            files.sort(key=lambda p: os.path.getsize(p), reverse=True)
            return files[0]
    raise RuntimeError("Could not determine downloaded file path.")


# --- Health server for Koyeb ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def run_health_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthHandler)
    logging.info("Health server running on port 8000")
    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    logging.info("Bot started and polling Telegram...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")
    except Exception:
        logging.exception("Bot polling stopped unexpectedly.")