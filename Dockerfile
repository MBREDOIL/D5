FROM python:3.10-slim-bullseye

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libmagic1 \
    libfreetype6 \
    libjbig2dec0 \
    libopenjp2-7 \
    libjpeg8 \
    libssl1.1 \
    libpng16-16 \
    libx11-6 \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application files
COPY . .

# Create necessary directories
RUN mkdir -p /app/downloads

# Environment variables
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

# Command to run your application
CMD ["python", "-m", "bot"]
