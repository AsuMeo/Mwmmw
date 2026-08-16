FROM python:3.11-alpine

# В Alpine пакеты ставятся за 2 секунды и весят в 10 раз меньше!
RUN apk add --no-cache ffmpeg curl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main_fixed.py .

CMD ["python", "main_fixed.py"]
