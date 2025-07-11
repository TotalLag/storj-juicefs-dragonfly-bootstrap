#!/usr/bin/env python3
"""
Server startup and main entry point for the async Redis proxy.
Includes Flask app for bootstrapping and main async server logic.
"""

import asyncio
import os
import subprocess
import logging
from flask import Flask, jsonify
from .config import Config, ConfigError, load_config, setup_logging
from .proxy import AsyncRedisProxy
from .metrics import start_metrics_server, stop_metrics_server

logger = logging.getLogger(__name__)

# --- Flask App for Bootstrapping ---

app = Flask(__name__)


@app.route('/up', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})


@app.route('/bootstrap', methods=['POST'])
def bootstrap():
    """Bootstrap JuiceFS with Storj configuration"""
    access_key = os.getenv('STORJ_ACCESS_KEY')
    secret_key = os.getenv('STORJ_SECRET_KEY')
    bucket_url = os.getenv('STORJ_BUCKET_URL')
    redis_url = os.getenv('REDIS_URL')
    
    missing_vars = []
    if not access_key: missing_vars.append('STORJ_ACCESS_KEY')
    if not secret_key: missing_vars.append('STORJ_SECRET_KEY')
    if not bucket_url: missing_vars.append('STORJ_BUCKET_URL')
    if not redis_url: missing_vars.append('REDIS_URL')
    
    if missing_vars:
        return jsonify({
            'success': False,
            'error': f'Missing required environment variables: {", ".join(missing_vars)}'
        }), 400
    
    cmd = [
        'juicefs', 'format',
        '--storage', 's3',
        '--bucket', bucket_url,
        '--access-key', access_key,
        '--secret-key', secret_key,
        f'{redis_url}/0',
        'sharedvol'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return jsonify({'success': True, 'output': result.stdout})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': e.stderr}), 500


@app.route('/metrics', methods=['GET'])
def stats():
    """Get proxy statistics (if proxy is running)"""
    # This would need to be connected to the running proxy instance
    # For now, return a placeholder
    return jsonify({
        'status': 'proxy_stats_not_available',
        'message': 'Statistics endpoint requires proxy integration'
    })


# --- Async Server Management ---

class AsyncServerManager:
    """Manages the async Redis proxy server lifecycle."""
    
    def __init__(self):
        self.proxy = None
        self.config = None
        self.metrics = None
        self._shutdown_event = asyncio.Event()
    
    async def initialize(self, config: Config):
        """Initialize the proxy server."""
        self.config = config
        self.proxy = AsyncRedisProxy(config)
        await self.proxy.initialize()
        
        # Start metrics server
        try:
            self.metrics = await start_metrics_server(self.proxy, metrics_port=self.config.metrics_port)
            logger.info("Prometheus metrics server initialized")
        except Exception as e:
            logger.warning(f"Failed to start metrics server: {e}")
        
        logger.info("Async Redis proxy initialized")
    
    async def start(self):
        """Start the proxy server."""
        if not self.proxy:
            raise RuntimeError("Proxy not initialized")
        
        try:
            # Start the proxy server
            await self.proxy.start_server()
        except asyncio.CancelledError:
            logger.info("Proxy server cancelled")
        except Exception as e:
            logger.error(f"Proxy server error: {e}")
            raise
    
    async def shutdown(self):
        """Shutdown the proxy server gracefully."""
        if self.proxy:
            logger.info("Shutting down proxy server...")
            await self.proxy.shutdown()
            logger.info("Proxy server shutdown complete")
        
        if self.metrics:
            logger.info("Shutting down metrics server...")
            await stop_metrics_server()
            logger.info("Metrics server shutdown complete")
        
        self._shutdown_event.set()
    
    async def wait_for_shutdown(self):
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()


# --- Signal Handling ---

def setup_signal_handlers(server_manager: AsyncServerManager):
    """Setup signal handlers for graceful shutdown."""
    import signal
    
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        asyncio.create_task(server_manager.shutdown())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# --- Main Entry Points ---

async def run_async_proxy():
    """Run the async Redis proxy server."""
    setup_logging()
    
    try:
        config = load_config()
        server_manager = AsyncServerManager()
        
        # Setup signal handlers
        setup_signal_handlers(server_manager)
        
        # Initialize and start the proxy
        await server_manager.initialize(config)
        
        logger.info("Starting async Redis proxy server...")
        
        # Create tasks for server and shutdown monitoring
        server_task = asyncio.create_task(server_manager.start())
        shutdown_task = asyncio.create_task(server_manager.wait_for_shutdown())
        
        # Wait for either server completion or shutdown signal
        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Ensure graceful shutdown
        await server_manager.shutdown()
        
    except KeyboardInterrupt:
        logger.info("Proxy stopped by user")
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Proxy failed: {e}")
        return 1
    
    return 0


def main():
    """Main entry point for the proxy (sync wrapper)."""
    try:
        return asyncio.run(run_async_proxy())
    except KeyboardInterrupt:
        logger.info("Proxy interrupted")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1


def run_flask_app():
    """Run the Flask app for bootstrapping (separate from proxy)."""
    setup_logging()
    logger.info("Starting Flask bootstrap app...")
    app.run(host='0.0.0.0', port=int(os.getenv('FLASK_PORT', '5000')))


if __name__ == "__main__":
    # This allows running the proxy directly for testing
    # In production, gunicorn will run the 'app' object for the web server,
    # and another process will run this script for the proxy.
    main()