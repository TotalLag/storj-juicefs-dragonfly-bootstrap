#!/usr/bin/env python3
"""
AUTH command interception logic for Redis proxy.
Handles authentication validation and credential transformation.
"""

import logging
from typing import Tuple
from .config import Config

logger = logging.getLogger(__name__)


class AuthInterceptor:
    """Handles AUTH command interception and credential transformation."""
    
    def __init__(self, config: Config):
        self.config = config
    
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