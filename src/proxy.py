#!/usr/bin/env python3
"""
Transparent Redis proxy that forwards raw TCP data and only intercepts AUTH commands
to handle username defaulting for JuiceFS compatibility.
"""

import socket
import threading
import logging
import time
import os
import subprocess
from typing import Optional, Tuple
from urllib.parse import urlparse

from flask import Flask, jsonify
from dotenv import load_dotenv

# --- Exceptions ---

class ConfigError(Exception):
    """Raised for invalid or missing configuration."""
    pass

# --- Configuration ---

class Config:
    """
    Immutable configuration loaded from environment variables.
    """
    def __init__(
        self,
        proxy_password: str,
        proxy_port: int,
        proxy_host: str,
        proxy_ipv6: bool,
        redis_url: Optional[str],
        redis_host: Optional[str],
        redis_port: Optional[int],
        redis_username: Optional[str],
        redis_password: Optional[str],
    ):
        self.proxy_password = proxy_password
        self.proxy_port = proxy_port
        self.proxy_host = proxy_host
        self.proxy_ipv6 = proxy_ipv6
        self.redis_url = redis_url
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_username = redis_username
        self.redis_password = redis_password

def load_config() -> Config:
    """
    Load and validate configuration from environment variables.
    """
    load_dotenv()

    proxy_password = os.getenv("PROXY_PASSWORD")
    proxy_port = int(os.getenv("PROXY_PORT", "6379"))
    proxy_host = os.getenv("PROXY_HOST", "::")
    proxy_ipv6 = os.getenv("PROXY_IPV6", "true").lower() in ("true", "1", "yes", "on")
    
    redis_url = os.getenv("REDIS_URL")
    redis_host = os.getenv("REDIS_HOST")
    redis_port_str = os.getenv("REDIS_PORT")
    redis_username = os.getenv("REDIS_USERNAME")
    redis_password = os.getenv("REDIS_PASSWORD")
    
    if redis_url:
        parsed = urlparse(redis_url)
        if not redis_host:
            redis_host = parsed.hostname
        if not redis_port_str:
            redis_port_str = str(parsed.port) if parsed.port else "6379"
        if not redis_username:
            redis_username = parsed.username or "default"
        if not redis_password:
            redis_password = parsed.password or ""

    if not proxy_password:
        raise ConfigError("PROXY_PASSWORD environment variable is missing")
    
    if not (redis_url or (redis_host and redis_port_str)):
        raise ConfigError("Redis upstream credentials are missing. Set REDIS_URL or REDIS_HOST and REDIS_PORT.")

    redis_port = int(redis_port_str) if redis_port_str else None

    return Config(
        proxy_password=proxy_password,
        proxy_port=proxy_port,
        proxy_host=proxy_host,
        proxy_ipv6=proxy_ipv6,
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_username=redis_username,
        redis_password=redis_password,
    )

# --- Logging ---

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Transparent Proxy ---

class TransparentRedisProxy:
    def __init__(self, config: Config):
        self.config = config
        
    def connect_upstream(self) -> Optional[socket.socket]:
        """Create connection to upstream Redis server."""
        try:
            host = self.config.redis_host
            port = self.config.redis_port
            
            logger.info(f"Attempting to connect to upstream Redis at {host}:{port}")
            
            addr_info = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            
            for family, socktype, proto, canonname, sockaddr in addr_info:
                try:
                    upstream = socket.socket(family, socktype, proto)
                    upstream.settimeout(10)
                    upstream.connect(sockaddr)
                    logger.info(f"Successfully connected to upstream Redis at {sockaddr}")
                    return upstream
                except Exception as e:
                    logger.warning(f"Failed to connect to {sockaddr}: {e}")
                    if upstream:
                        upstream.close()
            
            logger.error(f"Failed to connect to upstream Redis at {host}:{port}")
            return None

        except Exception as e:
            logger.error(f"Failed to connect to upstream Redis: {e}")
            return None

    def get_redis_password(self) -> str:
        """Extract Redis password from configuration"""
        return self.config.redis_password or ''

    def intercept_auth_command(self, data: bytes) -> Tuple[bytes, bool]:
        """
        Intercept AUTH commands, validate proxy password, and modify them if needed.
        Returns (modified_data, was_intercepted)
        """
        try:
            data_str = data.decode('utf-8', errors='ignore')
            
            # Check for HELLO command with AUTH (Redis 6+)
            if '*5\r\n$5\r\nhello\r\n' in data_str.lower() and '$4\r\nauth\r\n' in data_str.lower():
                logger.debug("Intercepted HELLO command with AUTH")
                parts = data_str.split('\r\n')
                if len(parts) >= 12:
                    password_len_idx = -3
                    password_idx = -2
                    if parts[password_len_idx].startswith('$'):
                        provided_password = parts[password_idx]
                        
                        # Validate proxy password
                        if provided_password != self.config.proxy_password:
                            logger.warning("Authentication failed: invalid proxy password")
                            return b"-ERR invalid password\r\n", True
                        
                        redis_password = self.get_redis_password()
                        redis_password_len = len(redis_password)
                        new_parts = parts[:]
                        new_parts[password_len_idx] = f'${redis_password_len}'
                        new_parts[password_idx] = redis_password
                        modified = '\r\n'.join(new_parts)
                        return modified.encode('utf-8'), True
            
            # Look for RESP AUTH command pattern - AUTH password only
            elif '*2\r\n$4\r\nAUTH\r\n$' in data_str:
                logger.debug("Intercepted AUTH password command")
                parts = data_str.split('\r\n')
                if len(parts) >= 6:
                    provided_password = parts[5]
                    
                    # Validate proxy password
                    if provided_password != self.config.proxy_password:
                        logger.warning("Authentication failed: invalid proxy password")
                        return b"-ERR invalid password\r\n", True
                    
                    # Replace with upstream Redis credentials (username + password)
                    redis_password = self.get_redis_password()
                    redis_username = self.config.redis_username or "default"
                    redis_password_len = len(redis_password)
                    redis_username_len = len(redis_username)
                    new_cmd = f"*3\r\n$4\r\nAUTH\r\n${redis_username_len}\r\n{redis_username}\r\n${redis_password_len}\r\n{redis_password}\r\n"
                    return new_cmd.encode('utf-8'), True
            
            # Look for RESP AUTH command pattern - AUTH username password
            elif '*3\r\n$4\r\nAUTH\r\n' in data_str:
                logger.debug("Intercepted AUTH username password command")
                parts = data_str.split('\r\n')
                if len(parts) >= 8:
                    # Extract password (last argument)
                    password_len_idx = len(parts) - 3
                    password_idx = len(parts) - 2
                    if parts[password_len_idx].startswith('$'):
                        provided_password = parts[password_idx]
                        
                        # Validate proxy password
                        if provided_password != self.config.proxy_password:
                            logger.warning("Authentication failed: invalid proxy password")
                            return b"-ERR invalid password\r\n", True
                        
                        # Replace with upstream Redis credentials (username + password)
                        redis_password = self.get_redis_password()
                        redis_username = self.config.redis_username or "default"
                        redis_password_len = len(redis_password)
                        redis_username_len = len(redis_username)
                        new_cmd = f"*3\r\n$4\r\nAUTH\r\n${redis_username_len}\r\n{redis_username}\r\n${redis_password_len}\r\n{redis_password}\r\n"
                        return new_cmd.encode('utf-8'), True
            
            if 'AUTH' in data_str.upper():
                logger.debug("Found AUTH command but pattern didn't match")
            
            return data, False
            
        except Exception as e:
            logger.warning(f"Error intercepting AUTH command: {e}")
            return data, False


    def forward_data(self, source: socket.socket, destination: socket.socket,
                    direction: str, intercept_auth: bool = False):
        """Forward data between sockets with optional AUTH interception"""
        try:
            while True:
                data = source.recv(4096)
                if not data:
                    break
                
                if intercept_auth and direction == "client->server":
                    data, was_intercepted = self.intercept_auth_command(data)
                    # If auth failed, the intercept method returns an error response
                    # Send it back to client and close connection
                    if was_intercepted and data.startswith(b"-ERR"):
                        source.send(data)
                        logger.info("Authentication failed, closing connection")
                        break
                
                destination.sendall(data)
                
        except Exception:
            pass
        finally:
            try:
                source.close()
                destination.close()
            except:
                pass

    def handle_client(self, client_socket: socket.socket, client_addr):
        """Handle a client connection with transparent proxying"""
        logger.info(f"New client connection from {client_addr}")
        
        upstream = self.connect_upstream()
        if not upstream:
            logger.error(f"Failed to connect to upstream for client {client_addr}")
            client_socket.close()
            return
        
        try:
            client_to_server = threading.Thread(
                target=self.forward_data,
                args=(client_socket, upstream, "client->server", True),
                daemon=True
            )
            
            server_to_client = threading.Thread(
                target=self.forward_data,
                args=(upstream, client_socket, "server->client", False),
                daemon=True
            )
            
            client_to_server.start()
            server_to_client.start()
            
            client_to_server.join()
            server_to_client.join()
            
            logger.info(f"Client {client_addr} disconnected")
            
        except Exception as e:
            logger.error(f"Error handling client {client_addr}: {e}")
        finally:
            try:
                client_socket.close()
                upstream.close()
            except:
                pass

    def start_server(self):
        """Start the transparent proxy server"""
        server_socket = None
        try:
            if self.config.proxy_ipv6:
                try:
                    server_socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    server_socket.bind((self.config.proxy_host, self.config.proxy_port))
                    logger.info(f"IPv6 dual-stack server listening on [{self.config.proxy_host}]:{self.config.proxy_port}")
                except Exception as e:
                    logger.warning(f"IPv6 bind failed: {e}, falling back to IPv4")
                    server_socket = None
            
            if not server_socket:
                server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                host = self.config.proxy_host if self.config.proxy_host != '::' else '0.0.0.0'
                server_socket.bind((host, self.config.proxy_port))
                logger.info(f"IPv4 server listening on {host}:{self.config.proxy_port}")
            
            server_socket.listen(100)
            logger.info("Transparent Redis proxy started successfully")
            
            while True:
                try:
                    client_socket, client_addr = server_socket.accept()
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, client_addr),
                        daemon=True
                    )
                    client_thread.start()
                except Exception as e:
                    logger.error(f"Error accepting client connection: {e}")
                    time.sleep(1)
                    
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise
        finally:
            if server_socket:
                server_socket.close()

# --- Flask App for Bootstrapping ---

app = Flask(__name__)

@app.route('/up', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/bootstrap', methods=['POST'])
def bootstrap():
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

# --- Main Entry Point ---

def main():
    """Main entry point for the proxy"""
    try:
        config = load_config()
        proxy = TransparentRedisProxy(config)
        proxy.start_server()
    except KeyboardInterrupt:
        logger.info("Proxy stopped by user")
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Proxy failed: {e}")
        return 1
    return 0

if __name__ == "__main__":
    # This allows running the proxy directly for testing
    # In production, gunicorn will run the 'app' object for the web server,
    # and another process will run this script for the proxy.
    main()