#!/usr/bin/env python3
"""
Text Leech Bot for Telegram (Telebot)
Flow:
- User sends /pw
- Bot asks for the text file (as document)
- Bot parses the file lines (one URL per line)
- Bot reports counts: video URLs vs PDF URLs
- User replies with the index number to download
- Bot asks for batch name
- Bot asks for desired quality (480 or 720)
- Bot asks for token (pw_token)
- Bot constructs final URL: https://anonymouspwplayer-25261acd1521.herokuapp.com/pw?url={url}&token={pw_token}
- For videos: uses yt_dlp to download the constructed URL (DASH/HLS/MPD)
- For PDFs: downloads directly via requests
- Finally uploads the file to Telegram and cleans up
Notes:
- Run this on Termux / Linux with Python 3.8+
- Install requirements from requirements.txt
- Keep your BOT_TOKEN safe. Do not commit it to public repos.
"""

import os
import tempfile
import logging
import telebot
import requests
from yt_dlp import YoutubeDL

# === CONFIG ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # set this in environment (Koyeb secret)
# If you prefer hardcoding (NOT recommended), place token string here:
# BOT_TOKEN = "123456:ABC-DEF..."

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required. Set it and restart the bot.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
logging.basicConfig(level=logging.INFO)

users_state = {}  # simple in-memory state per user_id

def is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith('.pdf') or 'application/pdf' in lower

def parse_urls_from_text(text: str):
    lines = [l.strip() for l in text.splitlines()]
    urls = [l for l in lines if l and (l.startswith('http://') or l.startswith('https://'))]
    return urls

@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    bot.reply_to(message, "Send /pw to begin: send a text file (one URL per line). Video URLs and PDF links supported.")

@bot.message_handler(commands=['pw'])
def cmd_pw(message):
    uid = message.from_user.id
    users_state[uid] = {"stage": "await_file"}
    bot.reply_to(message, "Please send the text file (as a Telegram document). It should contain one URL per line (videos or PDFs).")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    uid = message.from_user.id
    state = users_state.get(uid, {})
    if state.get("stage") != "await_file":
        bot.reply_to(message, "I wasn't expecting a file right now. Send /pw to start a new session.")
        return

    doc = message.document
    if doc.file_size > 10*1024*1024:
        # still allow but warn
        bot.reply_to(message, "Received file. (Large files may be slow to download.)")
    else:
        bot.reply_to(message, "Received file. Parsing...")

    file_info = bot.get_file(doc.file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = downloaded.decode('utf-8', errors='ignore')
    urls = parse_urls_from_text(text)
    if not urls:
        bot.reply_to(message, "No valid URLs found in the file. Make sure one URL per line and they start with http(s).")
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
    bot.reply_to(message, summary)

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    uid = message.from_user.id
    text = message.text.strip()
    state = users_state.get(uid)
    if not state:
        bot.reply_to(message, "Send /pw to start.")
        return

    stage = state.get("stage")
    if stage == "choosing_link":
        # expecting index
        try:
            idx = int(text)
        except:
            bot.reply_to(message, "Please send a valid number corresponding to the URL index.")
            return
        urls = state["urls"]
        if idx < 1 or idx > len(urls):
            bot.reply_to(message, "Index out of range. Pick a number from the list.")
            return
        chosen_url = urls[idx-1]
        state.update({"chosen_index": idx, "chosen_url": chosen_url, "stage": "ask_batch"})
        bot.reply_to(message, f"Selected URL #{idx}:\n{chosen_url}\n\nSend the batch/name you want to use (short text).")
        return

    if stage == "ask_batch":
        batch = text.replace('/', '_')[:64]
        state.update({"batch": batch, "stage": "ask_quality"})
        bot.reply_to(message, "Choose quality: send '480' or '720' (only these two).")
        return

    if stage == "ask_quality":
        if text not in ('480','720'):
            bot.reply_to(message, "Please reply with exactly '480' or '720'.")
            return
        state.update({"quality": text, "stage": "ask_token"})
        bot.reply_to(message, "Send the pw token string (the token used in the API). Keep it private.")
        return

    if stage == "ask_token":
        token = text.strip()
        state.update({"token": token, "stage": "downloading"})
        bot.reply_to(message, "Starting download... I will upload once completed. This may take long depending on file size.")
        # start download
        try:
            file_path = handle_download_and_prepare(uid, state)
        except Exception as e:
            logging.exception("Download error")
            bot.reply_to(message, f"Download failed: {e}")
            users_state.pop(uid, None)
            return

        # upload
        try:
            with open(file_path, 'rb') as f:
                fname = os.path.basename(file_path)
                bot.send_document(uid, f, caption=f"Batch: {state.get('batch', '')} — Source index: {state.get('chosen_index')}")
        except Exception as e:
            logging.exception("Upload error")
            bot.reply_to(message, f"Upload failed: {e}")
        finally:
            # cleanup
            try:
                os.remove(file_path)
            except: pass
            users_state.pop(uid, None)
        return

    bot.reply_to(message, "I didn't understand. Follow the flow: /pw -> send file -> pick index.")

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
        # yt_dlp returns entries or dict
        if 'requested_downloads' in info:
            # pick first
            downloads = info['requested_downloads']
            if downloads and downloads[0].get('filepath'):
                return downloads[0]['filepath']
        # fallback: try to locate a file in tmpdir
        files = [os.path.join(tmpdir,f) for f in os.listdir(tmpdir)]
        if files:
            # return largest
            files.sort(key=lambda p: os.path.getsize(p), reverse=True)
            return files[0]
    raise RuntimeError("Could not determine downloaded file path.")

if __name__ == "__main__":
    print("Bot started. Press Ctrl+C to stop.")
    bot.infinity_polling()
