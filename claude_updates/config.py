"""
Configuration management using Pydantic.

All settings are loaded from environment variables with sensible defaults.
"""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Service info
    service_name: str = "bloodbank"

    # RabbitMQ settings
    rabbit_url: str = "amqp://guest:guest@localhost:5672/"
    exchange_name: str = "amq.topic"

    # Redis settings (for correlation tracking)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    correlation_ttl_days: int = 30  # How long to keep correlation data

    # HTTP server settings
    http_host: str = "0.0.0.0"
    http_port: int = 8682

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
