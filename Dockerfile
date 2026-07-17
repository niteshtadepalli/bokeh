# CodeMender Orchestrator - Deployment Container for Cloud Run Jobs
# Base image: Python 3.11 slim
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Copy CodeMender CLI binary (downloaded by Cloud Build step into workspace root)
COPY cm /usr/local/bin/cm
RUN chmod +x /usr/local/bin/cm

# ==============================================================================
# Language Runtimes (BYOP Toolchain Configurations)
# Customize this section to match your target repository requirements.
# ==============================================================================

# RUNTIME: Node.js & npm (Active for JS/TS projects like juice-shop)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# RUNTIME: Go (Uncomment to enable for Go projects)
# COPY --from=golang:1.22 /usr/local/go /usr/local/go
# ENV PATH="/usr/local/go/bin:${PATH}"

# RUNTIME: Java JDK & Maven (Uncomment to enable for Maven projects)
# RUN apt-get update && apt-get install -y default-jdk maven && \
#     rm -rf /var/lib/apt/lists/*

# RUNTIME: PHP CLI (Uncomment to enable for PHP projects like DVWA)
RUN apt-get update && apt-get install -y --no-install-recommends php-cli && \
    rm -rf /var/lib/apt/lists/*

# ==============================================================================

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy orchestrator script
COPY orchestrator.py .

# Set entrypoint for Cloud Run Job execution
ENTRYPOINT ["python", "orchestrator.py"]
