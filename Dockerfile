FROM python:3.10-slim

WORKDIR /app
COPY . .
RUN apt-get update && apt-get install -y ffmpeg && \
    pip install --no-cache-dir -r requirements.txt

ENV BOT_TOKEN=""
CMD ["python3", "bot.py"]