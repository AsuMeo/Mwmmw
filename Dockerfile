FROM python:3.11-slim

# Устанавливаем системные зависимости (включая ffmpeg для скачивания видео с ютуб/тиктока)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main_fixed.py .

CMD ["python", "main_fixed.py"]
