#!/usr/bin/env python3
"""Simple event watcher for Bloodbank."""
import asyncio
import logging
from datetime import datetime
from rich.console import Console
from rich.table import Table

logging.basicConfig(level=logging.WARNING)

try:
    from event_producers.events.core.consumer import EventConsumer
except ImportError:
    print("Error: Run from bloodbank directory with: uv run python watch_events.py")
    exit(1)

console = Console()


async def handle_event(envelope):
    """Display event in clean format."""
    timestamp = datetime.now().strftime("%H:%M:%S")

    console.print(f"\n[bold cyan]{timestamp}[/] [yellow]{envelope.event_type}[/]")
    console.print(f"  ID: {envelope.event_id}")
    console.print(f"  Source: {envelope.source.app}@{envelope.source.host}")

    # Pretty print payload
    if hasattr(envelope, "payload"):
        for key, value in envelope.payload.items():
            if len(str(value)) > 60:
                value = str(value)[:57] + "..."
            console.print(f"  {key}: {value}")


async def main():
    consumer = EventConsumer(
        queue_name="event-watcher", binding_keys=["#"]  # All events
    )

    console.print("\n[bold green]Bloodbank Event Watcher[/]")
    console.print("[dim]Listening for all events... (Ctrl+C to stop)[/]\n")

    try:
        await consumer.start(handle_event)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped[/]")


if __name__ == "__main__":
    asyncio.run(main())
