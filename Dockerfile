FROM python:3.11-slim

# 安裝雲端無頭瀏覽器 Chromium 與 ChromeDriver
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-noto-cjk \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PYTHONUNBUFFERED=1
ENV PORT=7860

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 7860

# 使用單一 Worker 搭配多執行緒，確保背景監控執行緒只啟動一份
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:7860", "app:app"]
