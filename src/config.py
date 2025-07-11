#!/usr/bin/env python3
"""
Configuration module for Redis proxy.
Handles environment variable loading and validation.
"""

import os
import logging
from typing import Optional
from urllib.parse import urlparse
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""
    pass


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
        metrics_port: int,
        redis_url: Optional[str],
        redis_host: Optional[str],
        redis_port: Optional[int],
        redis_username: Optional[str],
        redis_password: Optional[str],
        redis_pool_size: int,
        redis_pool_timeout: int,
        redis_pool_keepalive: bool,
        redis_pool_keepalive_idle: int,
        redis_pool_keepalive_interval: int,
        redis_pool_keepalive_count: int,
        redis_pool_prewarm: int,
    ):
        self.proxy_password = proxy_password
        self.proxy_port = proxy_port
        self.proxy_host = proxy_host
        self.proxy_ipv6 = proxy_ipv6
        self.metrics_port = metrics_port
        self.redis_url = redis_url
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_username = redis_username
        self.redis_password = redis_password
        self.redis_pool_size = redis_pool_size
        self.redis_pool_timeout = redis_pool_timeout
        self.redis_pool_keepalive = redis_pool_keepalive
        self.redis_pool_keepalive_idle = redis_pool_keepalive_idle
        self.redis_pool_keepalive_interval = redis_pool_keepalive_interval
        self.redis_pool_keepalive_count = redis_pool_keepalive_count
        self.redis_pool_prewarm = redis_pool_prewarm


def get_log_level() -> int:
    """Get log level from environment variable with fallback to INFO"""
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    return log_levels.get(log_level_str, logging.INFO)


def setup_logging():
    """Setup logging configuration"""
    logging.basicConfig(
        level=get_log_level(),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


def load_config() -> Config:
    """
    Load and validate configuration from environment variables.
    """
    load_dotenv()

    proxy_password = os.getenv("PROXY_PASSWORD")
    proxy_port = int(os.getenv("PROXY_PORT", "6379"))
    proxy_host = os.getenv("PROXY_HOST", "::")
    metrics_port = int(os.getenv("METRICS_PORT", "9090"))
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
    pool_size = int(os.getenv("REDIS_POOL_SIZE", "1000"))
    pool_timeout = int(os.getenv("REDIS_POOL_TIMEOUT", "30"))
    pool_keepalive = os.getenv("REDIS_POOL_KEEPALIVE", "true").lower() in ("true", "1", "yes", "on")
    pool_keepalive_idle = int(os.getenv("REDIS_POOL_KEEPALIVE_IDLE", "1"))
    pool_keepalive_interval = int(os.getenv("REDIS_POOL_KEEPALIVE_INTERVAL", "3"))
    pool_keepalive_count = int(os.getenv("REDIS_POOL_KEEPALIVE_COUNT", "5"))
    pool_prewarm = int(os.getenv("REDIS_POOL_PREWARM", "50"))

    return Config(
        proxy_password=proxy_password,
        proxy_port=proxy_port,
        proxy_host=proxy_host,
        proxy_ipv6=proxy_ipv6,
        metrics_port=metrics_port,
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_username=redis_username,
        redis_password=redis_password,
        redis_pool_size=pool_size,
        redis_pool_timeout=pool_timeout,
        redis_pool_keepalive=pool_keepalive,
        redis_pool_keepalive_idle=pool_keepalive_idle,
        redis_pool_keepalive_interval=pool_keepalive_interval,
        redis_pool_keepalive_count=pool_keepalive_count,
        redis_pool_prewarm=pool_prewarm,
    )