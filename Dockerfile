FROM python:3.10-slim-bullseye

# Install system dependencies with backports for libcrypt
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libmagic1 \
    libfreetype6 \
    libjbig2dec0 \
    libopenjp2-7 \
    libjpeg62-turbo \
    libssl1.1 \
    libpng16-16 \
    libx11-6 \
    libmupdf-dev \
    libgl1 \
    libnss3 \
    libcrypt1 \
    poppler-utils \
    gcc \
    g++ \
    python3-dev \
    make \
    libjpeg-dev \
    zlib1g-dev \
    && apt-get install -y -t bullseye-backports libcrypt1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYMUPDF_SETUP_MUPDF_BUILD=0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads

CMD ["python", "-m", "bot"]
