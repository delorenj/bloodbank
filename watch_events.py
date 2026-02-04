#!/usr/bin/env python3
"""Simple event watcher for Bloodbank."""

import asyncio
import logging
from datetime import datetime

from rich.console import Console

logging.basicConfig(level=logging.WARNING)

try:
    # Consumer callback receives a decoded JSON dict
    from event_producers.events.core.consumer import EventConsumer
    from event_producers.events.base import EventEnvelope
except ImportError:
    print("Error: Run from bloodbank directory with: uv run python watch_events.py")
    raise

console = Console()


async def handle_event(payload: dict):
    """Display event in clean format."""
    timestamp = datetime.now().strftime("%H:%M:%S")

    try:
        envelope = EventEnvelope.model_validate(payload)
    except Exception:
        # If someone published a non-envelope JSON payload, show raw
        console.print(f"\n[bold cyan]{timestamp}[/] [red]UNPARSEABLE EVENT[/]")
        console.print(payload)
        return

    console.print(f"\n[bold cyan]{timestamp}[/] [yellow]{envelope.event_type}[/]")
    console.print(f"  ID: {envelope.event_id}")
    console.print(f"  Source: {envelope.source.app}@{envelope.source.host}")

    # Pretty print payload (dict payloads are common)
    try:
        items = envelope.payload.items() if hasattr(envelope.payload, "items") else []
        for key, value in items:
            if len(str(value)) > 60:
                value = str(value)[:57] + "..."
            console.print(f"  {key}: {value}")
    except Exception:
        console.print(f"  payload: {envelope.payload}")


async def main():
    # NOTE: EventConsumer signature is (service_name: str)
    consumer = EventConsumer("event-watcher")

    console.print("\n[bold green]Bloodbank Event Watcher[/]")
    console.print("[dim]Listening for all events... (Ctrl+C to stop)[/]\n")

    try:
        await consumer.start(handle_event, routing_keys=["#"])
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped[/]")


if __name__ == "__main__":
    asyncio.run(main())
