# Storj Bootstrap & Redis Proxy
This project originated when I ran out of space on Google Drive and needed to offload my photos and videos. JuiceFS is POSIX compatible and can be mounted on any machine to drag and drop files, acting like Google Drive or OneDrive.

## Project Overview

This project provides a transparent Redis proxy and a Flask-based worker designed to run on Fly.io. It serves two main purposes:

1.  **Transparent Redis Proxy**: Intercepts Redis `AUTH` commands to allow JuiceFS to connect to modern Redis services (like Upstash) that require both a username and password, while JuiceFS clients may only support sending a password.
2.  **Bootstrap Worker**: Exposes an HTTP endpoint (`/bootstrap`) to initialize a JuiceFS filesystem using Storj for object storage and a Redis-compatible service for metadata.

## Prerequisites

- A Fly.io account
- A Storj S3-compatible account
- An Upstash Redis account (or any other Redis-compatible service)
- [`flyctl`](https://fly.io/docs/hands-on/install-flyctl/) installed locally

## Configuration and Secret Management

All configuration is handled via environment variables, which should be set as secrets in your Fly.io application.

**Required Secrets:**

- `PROXY_PASSWORD`: A password that your clients (e.g., JuiceFS) will use to authenticate with this proxy.
- `REDIS_URL`: The connection string for your upstream Redis service. This should include the username, password, host, and port.
- `STORJ_ACCESS_KEY`: Your Storj S3 access key.
- `STORJ_SECRET_KEY`: Your Storj S3 secret key.
- `STORJ_BUCKET_URL`: The S3 bucket URL for your Storj bucket.

**Example `flyctl` commands:**

```bash
flyctl secrets set PROXY_PASSWORD="a-very-secure-password"
flyctl secrets set REDIS_URL="redis://default:your-redis-password@your-redis-host:port"
flyctl secrets set STORJ_ACCESS_KEY="YOUR_STORJ_ACCESS_KEY"
flyctl secrets set STORJ_SECRET_KEY="YOUR_STORJ_SECRET_KEY"
flyctl secrets set STORJ_BUCKET_URL="YOUR_STORJ_BUCKET_URL"
```

## Deployment

1.  **Launch the app on Fly.io:**
    This will create a new application and a `fly.toml` configuration file.
    ```bash
    flyctl launch --name your-app-name
    ```

2.  **Set your secrets:**
    Use the `flyctl secrets set` commands listed in the configuration section above.

3.  **Deploy to Fly.io:**
    ```bash
    flyctl deploy
    ```

### Multi-Process Architecture

The `fly.toml` file defines two processes that run in their machine:

-   **`app`**: Runs the transparent Redis proxy.
    -   Command: `python src/proxy.py`
-   **`web`**: Runs the Flask web server for the bootstrap endpoint.
    -   Command: `gunicorn src.proxy:app --bind 0.0.0.0:8080`

Fly.io is configured to expose two public services:
- The HTTP service on ports 80/443 is routed to the `web` process.
- The Redis service on port 6379 is routed to the `app` process.

## Usage

### Bootstrapping JuiceFS

Once deployed, you can initialize the JuiceFS volume by calling the `/bootstrap` endpoint. This only needs to be done once.

```bash
curl -X POST https://<your-app-name>.fly.dev/bootstrap
```

### Connecting with a Redis Client (e.g., JuiceFS)

Connect your Redis client to the proxy's public endpoint. Use the `PROXY_PASSWORD` you set as the password.

-   **Host**: `<your-app-name>.fly.dev`
-   **Port**: `6379`
-   **Password**: Your `PROXY_PASSWORD`

The proxy will automatically handle authenticating with your upstream Redis using the `REDIS_URL`.

### Mounting the JuiceFS Volume

To mount the JuiceFS filesystem on another machine:

1.  Install the JuiceFS client.
2.  Run the `juicefs mount` command, pointing it to your proxy's Redis endpoint:

    ```bash
    juicefs mount -d "redis://:<YOUR_PROXY_PASSWORD>@<your-app-name>.fly.dev:6379/0" /path/to/mount/point
    ```

## Local Development

1.  Create a `.env` file based on `.env.example`.
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Run the application. The `if __name__ == "__main__"` block in `src/proxy.py` will start the proxy directly. The Flask app for bootstrapping is also defined in the same file. To test locally, you would typically run the proxy directly.
    ```bash
    python src/proxy.py
    ```
    To test the web endpoint, you would use gunicorn as specified in the `fly.toml`.