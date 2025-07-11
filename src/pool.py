#!/usr/bin/env python3
"""
Async connection pool for Redis upstream connections.
Converted from threading-based SocketPool to asyncio-based AsyncConnectionPool.
"""

import asyncio
import socket
import logging
from typing import Optional, Dict, Any
from .config import Config

logger = logging.getLogger(__name__)


class AsyncConnectionPool:
    """Enhanced async connection pool for upstream Redis connections with health checking, keepalive, and monitoring."""
    
    def __init__(self, host: str, port: int, config: Config):
        self.host = host
        self.port = port
        self.size = config.redis_pool_size
        self.timeout = config.redis_pool_timeout
        self.keepalive = config.redis_pool_keepalive
        self.keepalive_idle = config.redis_pool_keepalive_idle
        self.keepalive_interval = config.redis_pool_keepalive_interval
        self.keepalive_count = config.redis_pool_keepalive_count
        self.prewarm_count = config.redis_pool_prewarm
        
        self.pool = asyncio.Queue(maxsize=self.size)
        self.lock = asyncio.Lock()
        
        # Pool statistics
        self.stats = {
            'created': 0,
            'reused': 0,
            'failed_health_checks': 0,
            'pool_hits': 0,
            'pool_misses': 0,
            'connections_closed': 0,
            'current_size': 0
        }
        
        # Background maintenance task
        self.maintenance_task = None
        self._shutdown = False
    
    async def initialize(self):
        """Initialize the pool and start background tasks."""
        # Start maintenance task
        self.maintenance_task = asyncio.create_task(self._maintenance_worker())
        
        # Prewarm the pool
        if self.prewarm_count > 0:
            await self._prewarm_pool()
    
    async def shutdown(self):
        """Shutdown the pool and cleanup resources."""
        self._shutdown = True
        if self.maintenance_task:
            self.maintenance_task.cancel()
            try:
                await self.maintenance_task
            except asyncio.CancelledError:
                pass
        
        # Close all connections in pool
        while not self.pool.empty():
            try:
                reader, writer = await asyncio.wait_for(self.pool.get(), timeout=0.1)
                writer.close()
                await writer.wait_closed()
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                break
    
    async def _prewarm_pool(self):
        """Prewarm the pool with initial connections."""
        logger.info(f"Prewarming connection pool with {self.prewarm_count} connections")
        tasks = []
        
        for i in range(min(self.prewarm_count, self.size)):
            tasks.append(self._create_connection_for_prewarm(i))
        
        # Use asyncio.gather for concurrent connection creation
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        successful = sum(1 for result in results if not isinstance(result, Exception))
        logger.info(f"Pool prewarmed with {successful} connections")
    
    async def _create_connection_for_prewarm(self, index: int):
        """Create a single connection for prewarming."""
        try:
            connection = await self._create_connection()
            if connection:
                await self.pool.put(connection)
                async with self.lock:
                    self.stats['current_size'] += 1
                    self.stats['created'] += 1
        except Exception as e:
            logger.warning(f"Failed to prewarm connection {index+1}: {e}")
    
    async def _create_connection(self) -> Optional[tuple]:
        """Create a new async connection with optimized settings."""
        try:
            # Use asyncio.wait_for for timeout control
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout
            )
            
            # Get the underlying socket for optimization
            sock = writer.get_extra_info('socket')
            if sock:
                # Set socket options for better performance
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                
                # Configure keepalive if enabled
                if self.keepalive:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    if hasattr(socket, 'TCP_KEEPIDLE'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, self.keepalive_idle)
                    if hasattr(socket, 'TCP_KEEPINTVL'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, self.keepalive_interval)
                    if hasattr(socket, 'TCP_KEEPCNT'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, self.keepalive_count)
            
            logger.debug(f"New upstream connection established to {self.host}:{self.port}")
            return reader, writer
            
        except Exception as e:
            logger.warning(f"Failed to create connection to {self.host}:{self.port}: {e}")
            return None
    
    async def _is_connection_healthy(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Check if a connection is still healthy and connected."""
        try:
            # Check if writer is closing or closed
            if writer.is_closing():
                logger.debug("Connection is closing")
                return False
            
            # Get the underlying socket for health check
            sock = writer.get_extra_info('socket')
            if not sock:
                return False
            
            # Use SO_ERROR to check for socket errors
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error != 0:
                logger.debug(f"Socket has error: {error}")
                return False
            
            # Send a PING command to check the connection's health
            try:
                writer.write(b'*1\r\n$4\r\nPING\r\n')
                await asyncio.wait_for(writer.drain(), timeout=1.0)
                
                # Wait for a PONG response
                response = await asyncio.wait_for(reader.read(1024), timeout=1.0)
                if response == b'+PONG\r\n':
                    return True
                else:
                    logger.warning(f"Unexpected response during health check: {response}")
                    return False
            except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as e:
                logger.debug(f"Connection health check failed: {e}")
                return False
            
        except Exception as e:
            logger.debug(f"Connection health check exception: {e}")
            return False
    
    async def get(self) -> tuple:
        """Get a connection from the pool, creating a new one if necessary."""
        # Try to get a connection from the pool
        while True:
            try:
                connection = self.pool.get_nowait()
                async with self.lock:
                    self.stats['pool_hits'] += 1
                    self.stats['current_size'] -= 1
                
                reader, writer = connection
                
                # Health check the connection
                if await self._is_connection_healthy(reader, writer):
                    async with self.lock:
                        self.stats['reused'] += 1
                    current_stats = await self.get_stats()
                    logger.debug(f"Reused healthy pooled connection to {self.host}:{self.port} (pool: {current_stats['pool_size']}/{current_stats['max_size']}, reused: {current_stats['reused']})")
                    return reader, writer
                else:
                    # Connection is unhealthy, close it and try next
                    async with self.lock:
                        self.stats['failed_health_checks'] += 1
                        self.stats['connections_closed'] += 1
                    logger.debug("Discarded unhealthy connection")
                    writer.close()
                    await writer.wait_closed()
                    continue
                    
            except asyncio.QueueEmpty:
                # Pool is empty, create a new connection
                async with self.lock:
                    self.stats['pool_misses'] += 1
                break
        
        # Create a new connection
        current_stats = await self.get_stats()
        logger.debug(f"Pool empty, creating new connection to {self.host}:{self.port} (pool: {current_stats['pool_size']}/{current_stats['max_size']}, total created: {current_stats['created']})")
        connection = await self._create_connection()
        if connection:
            async with self.lock:
                self.stats['created'] += 1
            updated_stats = await self.get_stats()
            logger.debug(f"New connection created (pool: {updated_stats['pool_size']}/{updated_stats['max_size']}, total created: {updated_stats['created']})")
            return connection
        else:
            raise ConnectionError(f"Failed to create connection to {self.host}:{self.port}")
    
    async def release(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Return a connection to the pool if there's space, otherwise close it."""
        try:
            if not reader or not writer:
                return
            
            # Health check before returning to pool
            if not await self._is_connection_healthy(reader, writer):
                async with self.lock:
                    self.stats['failed_health_checks'] += 1
                    self.stats['connections_closed'] += 1
                logger.debug("Closing unhealthy connection instead of returning to pool")
                writer.close()
                await writer.wait_closed()
                return
            
            # Try to return to pool
            try:
                self.pool.put_nowait((reader, writer))
                async with self.lock:
                    self.stats['current_size'] += 1
                logger.debug(f"Returned connection to pool (pool size: {self.pool.qsize()}/{self.size})")
            except asyncio.QueueFull:
                # Pool is full, close the connection
                async with self.lock:
                    self.stats['connections_closed'] += 1
                writer.close()
                await writer.wait_closed()
                logger.debug(f"Pool full, closed connection (pool size: {self.pool.qsize()}/{self.size})")
                
        except Exception as e:
            logger.warning(f"Error releasing connection: {e}")
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
    
    async def _maintenance_worker(self):
        """Background task to maintain pool health."""
        while not self._shutdown:
            try:
                await asyncio.sleep(60)  # Run maintenance every minute
                await self._cleanup_unhealthy_connections()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Pool maintenance error: {e}")
    
    async def _cleanup_unhealthy_connections(self):
        """Remove unhealthy connections from the pool."""
        cleaned = 0
        temp_connections = []
        
        # Extract all connections from pool
        while not self.pool.empty():
            try:
                connection = self.pool.get_nowait()
                temp_connections.append(connection)
            except asyncio.QueueEmpty:
                break
        
        # Check each connection and return healthy ones
        for reader, writer in temp_connections:
            if await self._is_connection_healthy(reader, writer):
                try:
                    self.pool.put_nowait((reader, writer))
                except asyncio.QueueFull:
                    writer.close()
                    await writer.wait_closed()
                    cleaned += 1
            else:
                writer.close()
                await writer.wait_closed()
                cleaned += 1
                async with self.lock:
                    self.stats['failed_health_checks'] += 1
                    self.stats['connections_closed'] += 1
                    self.stats['current_size'] -= 1
        
        if cleaned > 0:
            current_stats = await self.get_stats()
            logger.info(f"Pool maintenance: cleaned {cleaned} unhealthy connections (pool: {current_stats['pool_size']}/{current_stats['max_size']}, total created: {current_stats['created']}, failed health checks: {current_stats['failed_health_checks']})")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        async with self.lock:
            stats = self.stats.copy()
        stats['pool_size'] = self.pool.qsize()
        stats['max_size'] = self.size
        return stats