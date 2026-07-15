# CodeMender Orchestrator - Deployment Container for Cloud Run Jobs
# Base image: Python 3.11 slim
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Install CodeMender CLI binary
# If this download fails, the docker build fails immediately (no silent dummy fallback).
RUN curl -fsSL https://storage.googleapis.com/codemender-releases/latest/cm -o /usr/local/bin/cm && \
    chmod +x /usr/local/bin/cm

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy orchestrator script
COPY orchestrator.py .

# Set entrypoint for Cloud Run Job execution
ENTRYPOINT ["python", "orchestrator.py"]
