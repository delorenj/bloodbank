from pydantic import BaseModel
import os


class Settings(BaseModel):
    rabbit_url: str = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
    exchange_name: str = os.getenv("EXCHANGE_NAME", "bloodbank.events.v1")
    service_name: str = os.getenv("SERVICE_NAME", "bloodbank-api")
    environment: str = os.getenv("ENVIRONMENT", "dev")


settings = Settings()
