import os
print(f"ENV RABBIT_URL: {os.getenv('RABBIT_URL', 'NOT SET')}")

from event_producers.config import settings
print(f"settings.rabbit_url: {settings.rabbit_url}")
