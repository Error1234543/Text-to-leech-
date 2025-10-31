# Text Leech Bot (for Telegram) - Koyeb deploy

This repo contains a Telegram bot that:
- Accepts a text file (one URL per line)
- Shows counts of video vs PDF links
- Lets you pick which link to download by index
- Asks for batch name, quality (480/720), and token
- For video streams, it constructs:
  `https://anonymouspwplayer-25261acd1521.herokuapp.com/pw?url={url}&token={pw_token}`
  then passes that to `yt-dlp` to download.
- For PDFs, it downloads the PDF directly.

## Files
- `bot.py` - main bot script
- `requirements.txt` - Python dependencies
- `start.sh` - helper to run locally
- `README.md` - this file

## Setup (Koyeb)
1. Put the code on a git repo or upload the zip to Koyeb.
2. In Koyeb, set environment variable `BOT_TOKEN` to your Telegram bot token.
3. Ensure runtime supports Python 3.10+ and add a Procfile or command: `python3 bot.py`.
4. For large downloads, give enough disk quota (tmp usage). Koyeb functions may have limits — consider using a lightweight VM or container for large files.

## Notes & warnings
- Keep your BOT_TOKEN and any user tokens secret.
- This simple implementation stores per-user session state in memory. For production, use a persistent DB if you want restarts to preserve state.
- yt-dlp may require `ffmpeg` installed on the host for some formats. On Koyeb, ensure ffmpeg is available in the runtime image.

## License
Provided as-is. Modify per your needs.
