# Stage 1: Download and unpack DragonflyDB
FROM debian:stable-slim AS dragonflydb

RUN apt-get update && apt-get install -y curl tar unzip && rm -rf /var/lib/apt/lists/*

# Create folder and download tarball
RUN mkdir /dragonfly && \
    curl -L https://github.com/dragonflydb/dragonfly/releases/latest/download/dragonfly-x86_64.tar.gz -o dragonfly.tar.gz && \
    tar -xvf dragonfly.tar.gz && \
    mv dragonfly-x86_64 /dragonfly

# Stage 2: Main app image
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install JuiceFS
RUN curl -sSL https://d.juicefs.com/install | sh -

# Install AWS CLI
RUN pip install awscli

# Copy renamed Dragonfly binary
COPY --from=dragonflydb /dragonfly/dragonfly-x86_64 /usr/local/bin/dragonfly

# Make sure it’s executable
RUN chmod +x /usr/local/bin/dragonfly

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY start.sh .

EXPOSE 8080

ENTRYPOINT ["/app/start.sh"]
