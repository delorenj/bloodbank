import typer
import os
import subprocess
import json
import pathlib
import httpx
import sys
from typing import Optional, List, Dict, Any
from pathlib import Path
import importlib
from rich.console import Console
from rich.syntax import Syntax

# Fix Python path for installed tool to find local modules
# When running as installed script, add project root to path
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Note: config import is optional for core CLI functionality
try:
    from .config import settings
except ImportError:
    settings = None

app = typer.Typer(help="bloodbank CLI - Event-driven system for 33GOD ecosystem")
console = Console()


# ============================================================================
# Helper Functions
# ============================================================================


def get_events_dir() -> Path:
    """Get the events directory path."""
    current_file = Path(__file__).resolve()
    project_root = current_file.parent.parent
    return project_root / "events"


def discover_events() -> List[Dict[str, Any]]:
    """
    Auto-discover all event classes from the events/ directory structure.

    Returns:
        List of event metadata dicts with keys: name, class, routing_key, domain, is_command, module_path
    """
    events = []
    events_dir = get_events_dir()

    if not events_dir.exists():
        return events

    # Scan domain directories
    for domain_dir in events_dir.iterdir():
        if (
            not domain_dir.is_dir()
            or domain_dir.name.startswith("_")
            or domain_dir.name == "core"
        ):
            continue

        domain = domain_dir.name

        # Scan event modules within each domain
        for event_module_dir in domain_dir.iterdir():
            if not event_module_dir.is_dir() or event_module_dir.name.startswith("_"):
                continue

            # Look for the event class file
            event_files = list(event_module_dir.glob("*Event.py"))
            if not event_files:
                continue

            if len(event_files) > 1:
                console.print(
                    f"[yellow]Warning: Multiple event files found in {event_module_dir}, using first match[/yellow]"
                )

            event_file = event_files[0]
            event_class_name = event_file.stem

            try:
                # Dynamically import the event class
                module_path = (
                    f"events.{domain}.{event_module_dir.name}.{event_class_name}"
                )
                module = importlib.import_module(module_path)
                event_class = getattr(module, event_class_name)

                # Get metadata from the class
                routing_key = event_class.get_routing_key()
                is_command = event_class.is_command()

                # Construct mock file path correctly
                mock_filename = f"{event_class_name.replace('Event', '')}Mock.json"
                mock_file_path = event_module_dir / mock_filename

                events.append(
                    {
                        "name": event_class_name,
                        "class": event_class,
                        "routing_key": routing_key,
                        "domain": domain,
                        "is_command": is_command,
                        "module_path": module_path,
                        "mock_file": mock_file_path,
                    }
                )
            except (ImportError, AttributeError) as e:
                console.print(
                    f"[yellow]Warning: Could not load {event_class_name}: {e}[/yellow]"
                )
                continue

    return sorted(events, key=lambda x: (x["domain"], x["name"]))


def load_mock_data(event_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Load mock JSON data for an event.

    Args:
        event_info: Event metadata dict from discover_events()

    Returns:
        Parsed JSON dict or None if file doesn't exist
    """
    mock_file = event_info["mock_file"]
    if not mock_file.exists():
        return None

    try:
        with open(mock_file, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error parsing mock file {mock_file}: {e}[/red]")
        return None


def get_event_by_name(event_name: str) -> Optional[Dict[str, Any]]:
    """
    Find an event by name or routing key.

    Args:
        event_name: Event class name or routing key

    Returns:
        Event metadata dict or None if not found
    """
    events = discover_events()

    for event in events:
        if event["name"] == event_name or event["routing_key"] == event_name:
            return event

    return None


# ============================================================================
# CLI Commands
# ============================================================================


@app.command(name="list-events")
def list_events(
    domain: Optional[str] = typer.Option(
        None, "--domain", "-d", help="Filter by domain"
    ),
    event_type: Optional[str] = typer.Option(
        None, "--type", "-t", help="Filter by type (event or command)"
    ),
):
    """
    List all available events in the system.

    Events are organized by domain and can be filtered.
    """
    events = discover_events()

    if not events:
        console.print(
            "[yellow]No events found. Check that the events/ directory exists.[/yellow]"
        )
        return

    # Apply filters
    if domain:
        events = [e for e in events if e["domain"] == domain]

    if event_type:
        if event_type.lower() == "command":
            events = [e for e in events if e["is_command"]]
        elif event_type.lower() == "event":
            events = [e for e in events if not e["is_command"]]

    # Group by domain
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        by_domain.setdefault(event["domain"], []).append(event)

    # Display
    for domain_name, domain_events in by_domain.items():
        console.print(f"\n[bold cyan]Domain: {domain_name}[/bold cyan]")
        for event in domain_events:
            event_type_label = (
                "[yellow](command)[/yellow]" if event["is_command"] else ""
            )
            console.print(
                f"  - {event['routing_key']} ({event['name']}) {event_type_label}"
            )

    console.print(f"\n[dim]Total: {len(events)} event(s)[/dim]")


@app.command(name="list-commands")
def list_commands(
    domain: Optional[str] = typer.Option(
        None, "--domain", "-d", help="Filter by domain"
    ),
):
    """
    List all command events (mutable operations).

    This is a convenience command equivalent to: list-events --type command
    """
    list_events(domain=domain, event_type="command")


@app.command(name="show")
def show_event(event_name: str = typer.Argument(..., help="Event name or routing key")):
    """
    Show full event definition including schema and mock data.

    Examples:
        bloodbank show FirefliesTranscriptReadyEvent
        bloodbank show fireflies.transcript.ready
    """
    event_info = get_event_by_name(event_name)

    if not event_info:
        console.print(f"[red]Error: Event '{event_name}' not found.[/red]")
        console.print("[dim]Run 'bloodbank list-events' to see available events.[/dim]")
        raise typer.Exit(1)

    # Display event metadata
    console.print(f"\n[bold]Event: {event_info['name']}[/bold]")
    console.print(f"Domain: {event_info['domain']}")
    console.print(f"Routing Key: {event_info['routing_key']}")
    console.print(f"Type: {'Command' if event_info['is_command'] else 'Event'}")

    # Display schema
    console.print("\n[bold]Schema:[/bold]")
    event_class = event_info["class"]
    schema = event_class.model_json_schema()
    schema_json = json.dumps(schema, indent=2)
    syntax = Syntax(schema_json, "json", theme="monokai", line_numbers=True)
    console.print(syntax)

    # Display mock data
    mock_data = load_mock_data(event_info)
    if mock_data:
        console.print("\n[bold]Example (from mock):[/bold]")
        mock_json = json.dumps(mock_data, indent=2)
        mock_syntax = Syntax(mock_json, "json", theme="monokai", line_numbers=True)
        console.print(mock_syntax)
    else:
        console.print("\n[yellow]No mock data available for this event.[/yellow]")


@app.command(name="publish")
def publish_event(
    event_name: str = typer.Argument(..., help="Event name or routing key"),
    mock: bool = typer.Option(
        False, "--mock", "-m", help="Use mock data from JSON file"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print payload without publishing"
    ),
):
    """
    Publish an event to the event bus.

    Examples:
        bloodbank publish fireflies.transcript.ready --mock
        bloodbank publish AgentThreadPromptEvent --mock --dry-run
    """
    event_info = get_event_by_name(event_name)

    if not event_info:
        console.print(f"[red]Error: Event '{event_name}' not found.[/red]")
        console.print("[dim]Run 'bloodbank list-events' to see available events.[/dim]")
        raise typer.Exit(1)

    if not mock:
        console.print(
            "[red]Error: --mock flag is required (custom payloads not yet implemented)[/red]"
        )
        raise typer.Exit(1)

    # Load mock data
    mock_data = load_mock_data(event_info)
    if not mock_data:
        console.print(f"[red]Error: No mock data found for {event_info['name']}[/red]")
        raise typer.Exit(1)

    # Create event instance
    try:
        event_class = event_info["class"]
        event_instance = event_class(**mock_data)
    except Exception as e:
        console.print(f"[red]Error creating event instance: {e}[/red]")
        raise typer.Exit(1)

    # Wrap in EventEnvelope
    from events.core import EventEnvelope, Source, TriggerType
    import socket

    envelope = EventEnvelope(
        event_type=event_info["routing_key"],
        payload=event_instance.model_dump(),
        source=Source(
            host=socket.gethostname(),
            type=TriggerType.MANUAL,
            app="bloodbank-cli",
        ),
    )

    payload_json = envelope.model_dump_json(indent=2)

    if dry_run:
        console.print("\n[bold]Dry run - would publish:[/bold]")
        syntax = Syntax(payload_json, "json", theme="monokai", line_numbers=True)
        console.print(syntax)
        return

    # Publish to event bus
    try:
        # TODO: Implement actual RabbitMQ publishing
        # For now, use HTTP endpoint if available
        response = httpx.post(
            f"http://localhost:8682/events/{event_info['routing_key']}",
            json=json.loads(payload_json),
            timeout=5.0,
        )
        response.raise_for_status()
        console.print(
            f"[green]✓ Published {event_info['routing_key']} (event_id: {envelope.event_id})[/green]"
        )
    except httpx.HTTPError as e:
        console.print(f"[yellow]Warning: Could not publish via HTTP: {e}[/yellow]")
        console.print("[dim]Event payload:[/dim]")
        syntax = Syntax(payload_json, "json", theme="monokai", line_numbers=True)
        console.print(syntax)
    except Exception as e:
        console.print(f"[red]Error publishing event: {e}[/red]")
        raise typer.Exit(1)


@app.command(name="help")
def show_help():
    """
    Show comprehensive help organized by category.
    """
    console.print("\n[bold cyan]Bloodbank Event System Help[/bold cyan]")
    console.print("=" * 60)

    console.print("\n[bold]Events[/bold]")
    console.print(
        "Events are immutable messages that notify the system something happened."
    )
    console.print("Naming: <domain>.<entity>.<past-tense-action>")
    console.print("Example: fireflies.transcript.ready")

    console.print("\n[bold]Commands[/bold]")
    console.print("Commands are events that trigger mutations/actions.")
    console.print("Naming: <domain>.<entity>.<action>")
    console.print("Example: github.pr.merge")

    console.print("\n[bold]Publishing Events[/bold]")
    console.print("  bloodbank publish <event-name> --mock")
    console.print("  bloodbank publish <event-name> --mock --dry-run")
    console.print("\nExamples:")
    console.print("  bloodbank publish fireflies.transcript.ready --mock")
    console.print("  bloodbank publish AgentThreadPromptEvent --mock --dry-run")

    console.print("\n[bold]Adding Events[/bold]")
    console.print("1. Create event module: events/<domain>/<EventName>/")
    console.print("2. Add <EventName>Event.py with event class")
    console.print("3. Add <EventName>Mock.json with example data")
    console.print("4. Event class must inherit from BaseEvent or Command")
    console.print("5. Implement get_routing_key() class method")

    console.print("\n[bold]Extending Types[/bold]")
    console.print("- All events inherit from BaseEvent")
    console.print("- Commands inherit from Command (which extends BaseEvent)")
    console.print("- Events are wrapped in EventEnvelope for metadata")
    console.print("- Use Pydantic models for type safety")

    console.print("\n[bold]Available Commands[/bold]")
    console.print("  list-events [--domain DOMAIN] [--type TYPE]  List all events")
    console.print("  list-commands [--domain DOMAIN]              List command events")
    console.print("  show <event-name>                            Show event details")
    console.print("  publish <event-name> --mock                  Publish an event")
    console.print("  help                                         Show this help")

    console.print("\n[dim]For more information, see the documentation.[/dim]\n")


def detect_project_and_cwd():
    cwd = os.getcwd()
    # try resolve git root
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
        project = pathlib.Path(root).name
    except Exception:
        project = None
    return project, cwd


@app.command()
def publish_prompt(provider: str, model: str = "", prompt: str = typer.Argument(...)):
    project, cwd = detect_project_and_cwd()
    payload = {"provider": provider, "model": model or None, "prompt": prompt}
    r = httpx.post("http://localhost:8682/events/agent/thread/prompt", json=payload)
    typer.echo(r.json())


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def wrap(ctx: typer.Context, program: str, provider: str = "other"):
    """
    Wrap an LLM CLI and siphon its stdin/stdout.
    Usage: bb wrap claude -- <original args...>
    """
    project, cwd = detect_project_and_cwd()

    # capture input on stdin (prompt) if present
    # You can get fancier per tool, but this works generically for "echo '...' | tool"
    stdin_data = sys.stdin.read() if not sys.stdin.isatty() else None
    if stdin_data and stdin_data.strip():
        httpx.post(
            "http://localhost:8682/events/agent/thread/prompt",
            json={"provider": provider, "prompt": stdin_data, "model": None},
        )

    # run the original program pass-through
    cmd = [program] + ctx.args
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        if stdin_data:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        response_chunks = []
        for line in proc.stdout:
            response_chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()

    if response_chunks:
        httpx.post(
            "http://localhost:8682/events/agent/thread/response",
            json={
                "provider": provider,
                "prompt_id": "unknown",  # if you want, stash the ID from prompt call above
                "response": "".join(response_chunks),
            },
        )


if __name__ == "__main__":
    app()
