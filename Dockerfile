FROM python:3.11-slim

# Install FFmpeg
# Force rebuild - v2
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y fonts-noto && fc-cache -fv
    
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

EXPOSE 5000


CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "1", "--log-level", "debug", "--access-logfile", "-", "--error-logfile", "-", "app:app"]