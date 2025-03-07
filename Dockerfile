FROM python:3.10-bullseye

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libmagic1 \
    libmupdf-dev \
    libxcrypt-dev \
    && ln -s /usr/lib/x86_64-linux-gnu/libcrypt.so.1 /usr/lib/x86_64-linux-gnu/libcrypt.so.2 \
    && rm -rf /var/lib/apt/lists/*

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
