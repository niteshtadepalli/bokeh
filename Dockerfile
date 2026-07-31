# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# CodeMender Orchestrator - Deployment Container for Cloud Run Jobs
# Base image: Python 3.11 slim
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    psmisc \
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

# Copy orchestrator package and entrypoint script
COPY codemender_agent ./codemender_agent
COPY orchestrator.py .

# Set entrypoint for Cloud Run Job execution
ENTRYPOINT ["python", "orchestrator.py"]
