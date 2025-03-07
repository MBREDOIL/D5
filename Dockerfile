FROM python:3.10-slim-bullseye

# Install system dependencies
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
    libcrypt1 \
    && rm -rf /var/lib/apt/lists/*

# Create symbolic link for libcrypt.so.2
RUN ln -s /usr/lib/x86_64-linux-gnu/libcrypt.so.1 /usr/lib/x86_64-linux-gnu/libcrypt.so.2

WORKDIR /app

# Set environment variables before installing requirements
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYMUPDF_SETUP_MUPDF_BUILD=0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create necessary directories
RUN mkdir -p /app/downloads

CMD ["python", "-m", "bot"]
