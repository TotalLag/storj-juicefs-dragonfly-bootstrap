FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install system dependencies and JuiceFS
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install JuiceFS binary
RUN curl -sSL https://d.juicefs.com/install | sh -

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/

# Expose port for Flask
EXPOSE 8080

# Command to run the application using Gunicorn
CMD ["gunicorn", "src.proxy:app", "--bind", "0.0.0.0:$PORT"]