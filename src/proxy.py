#!/usr/bin/env python3
"""
Main async Redis proxy logic.
Handles client connections and data forwarding with AUTH interception.
"""

import asyncio
import logging
import socket
import time
from typing import Optional
from .config import Config
from .pool import AsyncConnectionPool
from .auth import AuthInterceptor

logger = logging.getLogger(__name__)


class AsyncRedisProxy:
    """Async transparent Redis proxy with AUTH interception."""
    
    def __init__(self, config: Config):
        self.config = config
        self.connection_pool = AsyncConnectionPool(
            host=self.config.redis_host,
            port=self.config.redis_port,
            config=config,
        )
        self.auth_interceptor = AuthInterceptor(config)
        self._shutdown = False
        self._connection_counter = 0
        self._connection_lock = asyncio.Lock()
    
    async def _get_next_connection_id(self) -> int:
        """Get the next unique connection ID."""
        async with self._connection_lock:
            self._connection_counter += 1
            return self._connection_counter
    
    async def initialize(self):
        """Initialize the proxy and its components."""
        await self.connection_pool.initialize()
    
    async def shutdown(self):
        """Shutdown the proxy and cleanup resources."""
        self._shutdown = True
        await self.connection_pool.shutdown()
    
    async def connect_upstream(self) -> Optional[tuple]:
        """Get connection to upstream Redis server from pool."""
        try:
            pool_stats = await self.connection_pool.get_stats()
            connection = await self.connection_pool.get()
            updated_stats = await self.connection_pool.get_stats()
            logger.debug(f"Borrowed upstream connection from pool (pool: {updated_stats['pool_size']}/{updated_stats['max_size']}, total created: {updated_stats['created']}, reused: {updated_stats['reused']})")
            return connection
        except Exception as e:
            pool_stats = await self.connection_pool.get_stats()
            logger.error(f"Failed to get upstream connection from pool: {e} (pool: {pool_stats['pool_size']}/{pool_stats['max_size']})")
            return None
    
    async def forward_data(self, source_reader: asyncio.StreamReader, dest_writer: asyncio.StreamWriter,
                          direction: str, intercept_auth: bool = False):
        """Forward data between streams with optional AUTH interception"""
        try:
            # Get metrics instance for data transfer recording
            metrics = None
            try:
                from .metrics import get_metrics
                metrics = get_metrics()
            except ImportError:
                pass
            
            while not self._shutdown:
                # Use asyncio.wait_for for timeout control
                try:
                    data = await asyncio.wait_for(source_reader.read(4096), timeout=30.0)
                    if not data:
                        break
                    
                    # Record data transfer metrics
                    if metrics:
                        if "client->server" in direction:
                            metrics.record_bytes_transferred(len(data), 'client_to_server')
                        elif "server->client" in direction:
                            metrics.record_bytes_transferred(len(data), 'server_to_client')
                    
                    if intercept_auth and direction == "client->server":
                        data, was_intercepted = self.auth_interceptor.intercept_auth_command(data)
                        # If auth failed, the intercept method returns an error response
                        # Send it back to client and close connection
                        if was_intercepted and data.startswith(b"-ERR"):
                            dest_writer.write(data)
                            await dest_writer.drain()
                            logger.info("Authentication failed, closing connection")
                            break
                    
                    dest_writer.write(data)
                    await dest_writer.drain()
                    
                except asyncio.TimeoutError:
                    # Timeout on read, check if connection is still alive
                    if dest_writer.is_closing():
                        break
                    continue
                    
        except Exception as e:
            logger.debug(f"Data forwarding stopped ({direction}): {e}")
    
    async def handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        """Handle a client connection with transparent proxying"""
        client_addr = client_writer.get_extra_info('peername')
        connection_id = await self._get_next_connection_id()
        pool_stats = await self.connection_pool.get_stats()
        connection_start_time = time.time()
        
        # Record metrics
        try:
            from .metrics import get_metrics
            metrics = get_metrics()
            if metrics:
                metrics.record_connection_accepted()
        except ImportError:
            pass  # Metrics not available
        
        logger.info(f"New client connection #{connection_id} from {client_addr} (pool: {pool_stats['pool_size']}/{pool_stats['max_size']}, active connections: {connection_id})")
        
        upstream_connection = await self.connect_upstream()
        if not upstream_connection:
            logger.error(f"Failed to connect to upstream for client #{connection_id} {client_addr}")
            
            # Record metrics for failed connection
            try:
                if metrics:
                    metrics.record_error('connection_error')
                    metrics.record_connection_closed(time.time() - connection_start_time)
            except:
                pass
            
            client_writer.close()
            await client_writer.wait_closed()
            return
        
        upstream_reader, upstream_writer = upstream_connection
        
        try:
            # Create concurrent tasks for bidirectional data forwarding
            client_to_server_task = asyncio.create_task(
                self.forward_data(client_reader, upstream_writer, "client->server", True)
            )
            
            server_to_client_task = asyncio.create_task(
                self.forward_data(upstream_reader, client_writer, "server->client", False)
            )
            
            # Use asyncio.gather for concurrent execution
            await asyncio.gather(
                client_to_server_task,
                server_to_client_task,
                return_exceptions=True
            )
            
            pool_stats = await self.connection_pool.get_stats()
            logger.info(f"Client #{connection_id} {client_addr} disconnected (pool: {pool_stats['pool_size']}/{pool_stats['max_size']})")
            
        except Exception as e:
            logger.error(f"Error handling client #{connection_id} {client_addr}: {e}")
            
            # Record error metrics
            try:
                if metrics:
                    metrics.record_error('forwarding_error')
            except:
                pass
        finally:
            # Record connection closed metrics
            connection_duration = time.time() - connection_start_time
            try:
                if metrics:
                    metrics.record_connection_closed(connection_duration)
            except:
                pass
            
            # Clean up client connection
            try:
                if not client_writer.is_closing():
                    client_writer.close()
                    await client_writer.wait_closed()
            except Exception:
                pass
            
            # Return upstream connection to pool
            try:
                await self.connection_pool.release(upstream_reader, upstream_writer)
                pool_stats = await self.connection_pool.get_stats()
                logger.debug(f"Returned upstream connection to pool for client #{connection_id} (pool: {pool_stats['pool_size']}/{pool_stats['max_size']})")
            except Exception:
                pass
    
    async def start_server(self):
        """Start the async transparent proxy server"""
        try:
            # Create a dual-stack socket manually for robust binding
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # This is the key to dual-stack support on most platforms
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                logger.info("Dual-stack socket configured.")
            except (AttributeError, OSError) as e:
                logger.warning(f"IPV6_V6ONLY not supported, may not have dual-stack: {e}")

            # Bind to the configured host and port
            host = self.config.proxy_host # Should be '::' for dual-stack
            port = self.config.proxy_port
            sock.bind((host, port))
            
            logger.info(f"Socket bound to [{host}]:{port}")

            server = await asyncio.start_server(
                self.handle_client,
                sock=sock
            )
            
            logger.info(f"Dual-stack server listening on [{host}]:{port}")
            logger.info("Async transparent Redis proxy started successfully")
            
            # Start serving
            async with server:
                await server.serve_forever()
                
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise
    
    async def get_pool_stats(self):
        """Get connection pool statistics."""
        return await self.connection_pool.get_stats()