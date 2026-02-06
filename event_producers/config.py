"""
Configuration management using Pydantic Settings.

All settings are loaded from environment variables with sensible defaults.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Service info
    service_name: str = "bloodbank"
    environment: str = "dev"

    # RabbitMQ settings
    rabbit_url: str = "amqp://guest:guest@rabbitmq:5672/"
    exchange_name: str = "bloodbank.events.v1"
    rabbit_publish_timeout: float = 30.0  # Timeout for publish operations in seconds

    # Redis settings (for correlation tracking)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    correlation_ttl_days: int = 30  # How long to keep correlation data

    # HTTP server settings
    http_host: str = "0.0.0.0"
    http_port: int = 8682

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
