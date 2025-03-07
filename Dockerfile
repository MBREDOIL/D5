FROM python:3.10-slim-bullseye

# System dependencies और build essentials install करें
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libmagic1 \
    gcc \
    libffi-dev \
    musl-dev \
    poppler-utils \
    libfreetype6 \
    libjbig2dec0 \
    libopenjp2-7 \
    libjpeg62-turbo \
    libssl1.1 \
    libpng16-16 \
    libx11-6 \
    libmupdf-dev \
    libcrypt1 \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Environment variables सेट करें
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYMUPDF_SETUP_MUPDF_BUILD=0

# Requirements install करें
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code copy करें
COPY . .

# Necessary directories बनाएं
RUN mkdir -p /app/downloads

# PDF processing tools verify करें
RUN pdftoppm -v && python3 -c "import hashlib; print('Hashlib available')"

# Multi-process management के लिए
CMD ["sh", "-c", "gunicorn app:app & python3 -m bot"]
