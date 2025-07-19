# Stage 1: Builder stage for downloading and preparing dependencies
FROM python:3.12-slim AS builder

# Install build-time dependencies
RUN apt-get update && apt-get install -y \
    curl \
    tar \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI
RUN pip install awscli

# Install DragonflyDB
RUN curl -L https://github.com/dragonflydb/dragonfly/releases/latest/download/dragonfly-x86_64.tar.gz -o dragonfly.tar.gz && \
    tar -xvf dragonfly.tar.gz && \
    mv dragonfly-x86_64 /usr/local/bin/dragonfly && \
    chmod +x /usr/local/bin/dragonfly && \
    rm dragonfly.tar.gz

# Install JuiceFS
RUN curl -sSL https://d.juicefs.com/install | sh -

# Stage 2: Final stage for the runtime environment
FROM python:3.12-slim-bookworm

# Set the working directory
WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    fuse3 \
    ca-certificates \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy binaries and dependencies from the builder stage
COPY --from=builder /usr/local/bin/dragonfly /usr/local/bin/dragonfly
COPY --from=builder /usr/local/bin/juicefs /usr/local/bin/juicefs
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/aws /usr/local/bin/aws

# Copy the startup script
COPY start.sh .

# Make the startup script executable
RUN chmod +x /app/start.sh

# Set the entrypoint for the container
ENTRYPOINT ["/app/start.sh"]
