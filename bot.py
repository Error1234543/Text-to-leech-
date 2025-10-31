#!/usr/bin/env python3
"""
Fixed Text Leech Bot for Telegram (Telebot)
- Uses a safe reply helper (avoids crash when reply target is missing)
- Starts a simple HTTP health server on port 8000 (for Koyeb health checks)
- Polling-based bot (suitable for Termux / simple containers)
"""

import os
import tempfile
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
import requests
from yt_dlp import YoutubeDL

# === CONFIG ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # set this in environment (Koyeb secret)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required. Set it and restart the bot.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
logging.basicConfig(level=logging.INFO)

users_state = {}  # simple in-memory state per user_id

def safe_send(message, text):
    """
    Try to reply_to the message; if Telegram returns an error (e.g. original message deleted),
    fall back to send_message so bot does NOT crash.
    """
    try:
        bot.reply_to(message, text)
    except Exception as e:
        try:
            bot.send_message(message.chat.id, text)
        except Exception:
            # last-resort: log and ignore
            logging.exception("Failed to send message to user.")

def is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith('.pdf') or 'application/pdf' in lower

def parse_urls_from_text(text: str):
    lines = [l.strip() for l in text.splitlines()]
    urls = [l for l in lines if l and (l.startswith('http://') or l.startswith('https://'))]
    return urls

@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    safe_send(message, "Send /pw to begin: send a text file (one URL per line). Video URLs and PDF links supported.")

@bot.message_handler(commands=['pw'])
def cmd_pw(message):
    uid = message.from_user.id
    users_state[uid] = {"stage": "await_file"}
    safe_send(message, "Please send the text file (as a Telegram document). It should contain one URL per line (videos or PDFs).")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    uid = message.from_user.id
    state = users_state.get(uid, {})
    if state.get("stage") != "await_file":
        safe_send(message, "I wasn't expecting a file right now. Send /pw to start a new session.")
        return

    doc = message.document
    try:
        if doc.file_size > 10*1024*1024:
            safe_send(message, "Received file. (Large files may be slow to download.)")
        else:
            safe_send(message, "Received file. Parsing...")
    except Exception:
        # If doc.metadata missing or access issue, continue gracefully
        safe_send(message, "Received file. Parsing...")

    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = downloaded.decode('utf-8', errors='ignore')
    urls = parse_urls_from_text(text)
    if not urls:
        safe_send(message, "No valid URLs found in the file. Make sure one URL per line and they start with http(s).")
        users_state.pop(uid, None)
        return

    # classify
    pdfs = [u for u in urls if is_pdf_url(u)]
    videos = [u for u in urls if not is_pdf_url(u)]
    state.update({
        "stage": "choosing_link",
        "urls": urls,
        "pdfs": pdfs,
        "videos": videos
    })

    summary = f"Found {len(urls)} URLs — {len(videos)} videos, {len(pdfs)} PDFs.\n\n"
    summary += "List of URLs (index : type : short URL):\n"
    for i,u in enumerate(urls, start=1):
        t = "PDF" if is_pdf_url(u) else "VIDEO"
        summary += f"{i}. [{t}] {u[:80]}\n"
    summary += "\nReply with the number of the link you want to download (e.g. 1)."
    safe_send(message, summary)

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
        # expecting index
        try:
            idx = int(text)
        except:
            safe_send(message, "Please send a valid number corresponding to the URL index.")
            return
        urls = state["urls"]
        if idx < 1 or idx > len(urls):
            safe_send(message, "Index out of range. Pick a number from the list.")
            return
        chosen_url = urls[idx-1]
        state.update({"chosen_index": idx, "chosen_url": chosen_url, "stage": "ask_batch"})
        safe_send(message, f"Selected URL #{idx}:\n{chosen_url}\n\nSend the batch/name you want to use (short text).")
        return

    if stage == "ask_batch":
        batch = text.replace('/', '_')[:64]
        state.update({"batch": batch, "stage": "ask_quality"})
        safe_send(message, "Choose quality: send '480' or '720' (only these two).")
        return

    if stage == "ask_quality":
        if text not in ('480','720'):
            safe_send(message, "Please reply with exactly '480' or '720'.")
            return
        state.update({"quality": text, "stage": "ask_token"})
        safe_send(message, "Send the pw token string (the token used in the API). Keep it private.")
        return

    if stage == "ask_token":
        token = text.strip()
        state.update({"token": token, "stage": "downloading"})
        safe_send(message, "Starting download... I will upload once completed. This may take long depending on file size.")
        # start download
        try:
            file_path = handle_download_and_prepare(uid, state)
        except Exception as e:
            logging.exception("Download error")
            safe_send(message, f"Download failed: {e}")
            users_state.pop(uid, None)
            return

        # upload
        try:
            with open(file_path, 'rb') as f:
                fname = os.path.basename(file_path)
                bot.send_document(uid, f, caption=f"Batch: {state.get('batch', '')} — Source index: {state.get('chosen_index')}")
        except Exception as e:
            logging.exception("Upload error")
            safe_send(message, f"Upload failed: {e}")
        finally:
            # cleanup
            try:
                os.remove(file_path)
            except: pass
            users_state.pop(uid, None)
        return

    safe_send(message, "I didn't understand. Follow the flow: /pw -> send file -> pick index.")

def handle_download_and_prepare(uid: int, state: dict) -> str:
    """
    Returns local file path of downloaded file.
    """
    chosen_url = state["chosen_url"]
    token = state["token"]
    quality = state["quality"]
    batch = state["batch"]

    # construct final API URL
    api_base = "https://anonymouspwplayer-25261acd1521.herokuapp.com/pw"
    final_url = f"{api_base}?url={chosen_url}&token={token}"
    logging.info("Final URL: %s", final_url)

    tmpdir = tempfile.mkdtemp(prefix="tlb_")
    # decide if PDF
    if is_pdf_url(chosen_url):
        # For PDFs, download original link directly (user requested that pdf links download directly)
        target_url = chosen_url
        local_name = f"{batch}_{state['chosen_index']}.pdf"
        out_path = os.path.join(tmpdir, local_name)
        with requests.get(target_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, 'wb') as fw:
                for chunk in r.iter_content(chunk_size=1024*64):
                    if chunk:
                        fw.write(chunk)
        return out_path

    # For videos, use the proxy API final_url (as requested)
    ydl_opts = {
        'outtmpl': os.path.join(tmpdir, f"{batch}_%(id)s.%(ext)s"),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        # retry options
        'retries': 3,
    }

    # set format preference based on quality
    if quality == '720':
        ydl_opts['format'] = "bestvideo[height<=720]+bestaudio/best[height<=720]"
    else:
        ydl_opts['format'] = "bestvideo[height<=480]+bestaudio/best[height<=480]"

    # Use yt_dlp to download the final_url
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(final_url, download=True)
        # find downloaded filename
        if isinstance(info, dict) and 'requested_downloads' in info:
            downloads = info['requested_downloads']
            if downloads and downloads[0].get('filepath'):
                return downloads[0]['filepath']
        # fallback: try to locate a file in tmpdir
        files = [os.path.join(tmpdir,f) for f in os.listdir(tmpdir)]
        if files:
            files.sort(key=lambda p: os.path.getsize(p), reverse=True)
            return files[0]
    raise RuntimeError("Could not determine downloaded file path.")

# --- Health server for Koyeb (port 8000) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    server = HTTPServer(("0.0.0.0", 8000), HealthHandler)
    logging.info("Starting health server on port 8000")
    try:
        server.serve_forever()
    except Exception:
        logging.exception("Health server stopped")

if __name__ == "__main__":
    # Start health server in background so Koyeb TCP checks pass
    threading.Thread(target=run_health_server, daemon=True).start()
    logging.info("Bot started. Polling Telegram...")
    # use polling with reasonable timeouts
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logging.info("Stopping on keyboard interrupt")
    except Exception:
        logging.exception("Polling stopped due to exception")
