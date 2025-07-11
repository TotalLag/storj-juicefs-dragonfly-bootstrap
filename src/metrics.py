#!/usr/bin/env python3
"""
Prometheus metrics integration for the Redis proxy.
Provides connection and pool statistics via Prometheus metrics.
"""

import asyncio
import logging
from typing import Optional
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from .proxy import AsyncRedisProxy

logger = logging.getLogger(__name__)


class ProxyMetrics:
    """Prometheus metrics collector for Redis proxy."""
    
    def __init__(self, proxy: AsyncRedisProxy, metrics_port: int):
        self.proxy = proxy
        self.metrics_port = metrics_port
        self.metrics_server = None
        self.metrics_thread = None
        
        # Connection metrics
        self.connections_total = Gauge(
            'redis_proxy_connections_total',
            'Total number of client connections',
            ['status']  # 'accepted', 'rejected'
        )
        
        self.active_connections = Gauge(
            'redis_proxy_active_connections',
            'Current number of active client connections'
        )
        
        self.connection_duration = Gauge(
            'redis_proxy_connection_duration_seconds',
            'Duration of client connections in seconds'
        )
        
        # Pool metrics
        self.pool_connections_reused = Gauge(
            'redis_proxy_pool_connections_reused_total',
            'Total number of upstream connections reused from pool'
        )
        
        self.pool_connections_failed_health_checks = Gauge(
            'redis_proxy_pool_connections_failed_health_checks_total',
            'Total number of connections that failed health checks'
        )
        
        self.pool_connections_closed = Gauge(
            'redis_proxy_pool_connections_closed_total',
            'Total number of connections closed'
        )
        
        self.pool_size_current = Gauge(
            'redis_proxy_pool_size_current',
            'Current number of connections in the pool'
        )
        
        self.pool_size_max = Gauge(
            'redis_proxy_pool_size_max',
            'Maximum pool size configured'
        )
        
        self.pool_size_available = Gauge(
            'redis_proxy_pool_size_available',
            'Number of connections currently available in the pool'
        )
        
        self.pool_hits = Gauge(
            'redis_proxy_pool_hits_total',
            'Total number of pool hits (connections retrieved from pool)'
        )
        
        self.pool_misses = Gauge(
            'redis_proxy_pool_misses_total',
            'Total number of pool misses (new connections created)'
        )
        
        # Data transfer metrics
        self.bytes_transferred = Gauge(
            'redis_proxy_bytes_transferred_total',
            'Total bytes transferred',
            ['direction']  # 'client_to_server', 'server_to_client'
        )
        
        # Error metrics
        self.errors_total = Gauge(
            'redis_proxy_errors_total',
            'Total number of errors',
            ['type']  # 'connection_error', 'auth_error', 'proxy_error', 'connection_release_error'
        )
    
    async def start_metrics_server(self):
        """Start the Prometheus metrics HTTP server."""
        try:
            self.metrics_server, self.metrics_thread = start_http_server(self.metrics_port)
            logger.info(f"Prometheus metrics server started on port {self.metrics_port}")
            
            # Start background task to update metrics
            asyncio.create_task(self._update_metrics_loop())
            
        except Exception as e:
            logger.error(f"Failed to start metrics server: {e}")
            raise
    
    async def stop_metrics_server(self):
        """Stop the Prometheus metrics HTTP server."""
        if self.metrics_server:
            try:
                self.metrics_server.shutdown()
                if self.metrics_thread:
                    self.metrics_thread.join()
                logger.info("Prometheus metrics server stopped")
            except Exception as e:
                logger.warning(f"Error stopping metrics server: {e}")
    
    async def _update_metrics_loop(self):
        """Background task to periodically update metrics from proxy stats."""
        while True:
            try:
                await asyncio.sleep(10)  # Update every 10 seconds
                await self._update_pool_metrics()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error updating metrics: {e}")
    
    async def _update_pool_metrics(self):
        """Update pool-related metrics from current pool stats."""
        try:
            if self.proxy and self.proxy.connection_pool:
                stats = await self.proxy.get_pool_stats()
                
                # Update pool metrics
                self.pool_size_current.set(stats.get('current_size', 0))
                self.pool_size_max.set(stats.get('max_size', 0))
                self.pool_size_available.set(stats.get('pool_size', 0))  # Fix: Add pool_size_available metric
                
                # Update counters (these are cumulative, so we set them to current values)
                self.pool_connections_reused.set(stats.get('reused', 0))
                self.pool_connections_failed_health_checks.set(stats.get('failed_health_checks', 0))
                self.pool_connections_closed.set(stats.get('connections_closed', 0))
                self.pool_hits.set(stats.get('pool_hits', 0))
                self.pool_misses.set(stats.get('pool_misses', 0))
                
        except Exception as e:
            logger.debug(f"Error updating pool metrics: {e}")
    
    def record_connection_accepted(self):
        """Record a new client connection accepted."""
        self.connections_total.labels(status='accepted').inc()
        self.active_connections.inc()
    
    def record_connection_rejected(self):
        """Record a client connection rejected."""
        self.connections_total.labels(status='rejected').inc()
    
    def record_connection_closed(self, duration_seconds: float):
        """Record a client connection closed."""
        self.active_connections.dec()
        self.connection_duration.set(duration_seconds)
    
    def record_bytes_transferred(self, bytes_count: int, direction: str):
        """Record bytes transferred."""
        self.bytes_transferred.labels(direction=direction).inc(bytes_count)
    
    def record_error(self, error_type: str):
        """Record an error occurrence."""
        self.errors_total.labels(type=error_type).inc()


# Global metrics instance
_metrics_instance: Optional[ProxyMetrics] = None


def get_metrics() -> Optional[ProxyMetrics]:
    """Get the global metrics instance."""
    return _metrics_instance


def initialize_metrics(proxy: AsyncRedisProxy, metrics_port: int) -> ProxyMetrics:
    """Initialize the global metrics instance."""
    global _metrics_instance
    _metrics_instance = ProxyMetrics(proxy, metrics_port)
    return _metrics_instance


async def start_metrics_server(proxy: AsyncRedisProxy, metrics_port: int):
    """Start the metrics server."""
    metrics = initialize_metrics(proxy, metrics_port)
    await metrics.start_metrics_server()
    return metrics


async def stop_metrics_server():
    """Stop the metrics server."""
    if _metrics_instance:
        await _metrics_instance.stop_metrics_server()