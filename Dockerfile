FROM python:3.10-slim-bullseye

# System dependencies और libcrypt1 legacy संस्करण के लिए
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
    libnss3 \
    libnspr4 \
    && apt-get install -y -t bullseye-backports libcrypt1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYMUPDF_SETUP_MUPDF_BUILD=0

# Requirements install करें (gunicorn को जोड़ें)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Application code copy करें
COPY . .

# Necessary directories बनाएं
RUN mkdir -p /app/downloads

# PDF tools और libraries verify करें
RUN pdftoppm -v && \
    python3 -c "import hashlib; print('Hashlib available')" && \
    ldd /usr/local/lib/python3.10/site-packages/fitz/_fitz.cpython-310-x86_64-linux-gnu.so

# Multi-process management के लिए supervisord का उपयोग करें
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT & python3 -m bot"]
