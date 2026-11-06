FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg p7zip-full curl unzip ca-certificates gcc p7zip-full \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://rclone.org/install.sh | bash

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "main.py"]
