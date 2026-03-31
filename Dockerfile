FROM python:3.12-slim

# Install ffmpeg and timezone data
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the monitor script
COPY webcam_monitor.py .

# Log and chart output will go to /data (mount a volume here)
RUN mkdir -p /data
ENV MONITOR_DATA_DIR=/data

# Default: run in continuous loop mode
CMD ["python", "webcam_monitor.py", "--loop"]
