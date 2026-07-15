FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
# - gcc, libpq-dev: postgres/asyncpg
# - tesseract-ocr, poppler-utils: OCR for scanned PDFs
# - ffmpeg, libsndfile1: audio processing for faster-whisper
# - espeak-ng: pyttsx3 TTS engine on Linux
# - curl: for health checks and piper install
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc libpq-dev \
        tesseract-ocr poppler-utils \
        ffmpeg libsndfile1 \
        espeak-ng espeak-ng-data \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install piper TTS binary
# piper releases: https://github.com/rhasspy/piper/releases
ARG PIPER_VERSION=2023.11.14-2
RUN mkdir -p /opt/piper && \
    curl -sL "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz" \
    | tar -xz -C /opt/piper && \
    ln -sf /opt/piper/piper /usr/local/bin/piper && \
    mkdir -p /opt/piper/models

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
