import asyncio
import typer
import os
import subprocess
import json
import pathlib
import httpx
import sys
import socket
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pathlib import Path
from uuid import UUID, uuid4
from rich.console import Console
from rich.syntax import Syntax

from event_producers.events import EventEnvelope, Source, TriggerType, create_envelope
from event_producers.events.registry import get_registry
from event_producers.rabbit import Publisher
from event_producers.schema_validator import validate_event
from event_producers.config import settings

# Fix Python path for installed tool to find local modules
# When running as installed script, add project root to path
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

app = typer.Typer(help="bloodbank CLI - Event-driven system for 33GOD ecosystem")
console = Console()


# ============================================================================
# Helper Functions
# ============================================================================


def discover_events() -> List[Dict[str, Any]]:
    """
    Auto-discover all event classes from the registry.

    Returns:
        List of event metadata dicts with keys: name, class, routing_key, domain, is_command, module_path
    """
    events: List[Dict[str, Any]] = []
    registry = get_registry()
    registry.auto_discover_domains()

    for domain_name, domain in registry.domains.items():
        for routing_key in domain.list_events():
            payload_class = domain.payload_types[routing_key]
            events.append(
                {
                    "name": payload_class.__name__,
                    "class": payload_class,
                    "routing_key": routing_key,
                    "domain": domain_name,
                    "is_command": False,
                    "module_path": payload_class.__module__,
                    "mock_file": None,
                }
            )

    return sorted(events, key=lambda x: (x["domain"], x["name"]))


def load_mock_data(event_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Load mock JSON data for an event.

    Args:
        event_info: Event metadata dict from discover_events()

    Returns:
        Parsed JSON dict or None if file doesn't exist
    """
    mock_file = event_info.get("mock_file")
    if not mock_file:
        return None
    if not mock_file.exists():
        return None

    try:
        with open(mock_file, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error parsing mock file {mock_file}: {e}[/red]")
        return None


def _load_json_arg(value: str) -> Dict[str, Any]:
    """
    Load JSON from an inline string, @file, or stdin (-).
    """
    if value == "-":
        data = sys.stdin.read()
    elif value.startswith("@"):
        data = Path(value[1:]).read_text()
    else:
        data = value
    return json.loads(data)


def _parse_uuid_list(values: List[str]) -> List[UUID]:
    out: List[UUID] = []
    for v in values:
        try:
            out.append(UUID(v))
        except ValueError:
            console.print(f"[yellow]Skipping invalid UUID: {v}[/yellow]")
    return out


def _resolve_event(event_name: str) -> Optional[Dict[str, Any]]:
    """
    Resolve event by routing key or class name using registry discovery.
    """
    events = discover_events()
    for event in events:
        if event["routing_key"] == event_name or event["name"] == event_name:
            return event
    return None


async def _publish_envelope(routing_key: str, envelope: EventEnvelope) -> None:
    """
    Publish an event envelope to RabbitMQ with timeout handling.
    
    Args:
        routing_key: The routing key for the message
        envelope: The event envelope to publish
        
    Raises:
        asyncio.TimeoutError: If the publish operation exceeds the configured timeout
        RuntimeError: If connection or publishing fails
    """
    publisher = Publisher(enable_correlation_tracking=True)
    
    try:
        # Wrap the entire publish operation with timeout
        await asyncio.wait_for(
            _do_publish(publisher, routing_key, envelope),
            timeout=settings.rabbit_publish_timeout
        )
    except asyncio.TimeoutError:
        # Ensure cleanup on timeout
        await publisher.close()
        raise asyncio.TimeoutError(
            f"RabbitMQ publish operation timed out after {settings.rabbit_publish_timeout} seconds. "
            "Check RabbitMQ connectivity and consider increasing RABBIT_PUBLISH_TIMEOUT."
        )
    except Exception:
        # Ensure cleanup on any error
        await publisher.close()
        raise


async def _do_publish(publisher: Publisher, routing_key: str, envelope: EventEnvelope) -> None:
    """Helper function to perform the actual publish operation."""
    await publisher.start()
    await publisher.publish(
        routing_key=routing_key,
        body=envelope.model_dump(mode="json"),
        event_id=envelope.event_id,
        parent_event_ids=envelope.correlation_ids,
    )
    await publisher.close()


def get_event_by_name(event_name: str) -> Optional[Dict[str, Any]]:
    """
    Find an event by name or routing key.

    Args:
        event_name: Event class name or routing key

    Returns:
        Event metadata dict or None if not found
    """
    return _resolve_event(event_name)


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
    payload_json: Optional[str] = typer.Option(
        None,
        "--json",
        "-j",
        help="Payload JSON string, @file, or '-' for stdin",
    ),
    payload_file: Optional[Path] = typer.Option(
        None,
        "--payload-file",
        help="Path to JSON payload file",
    ),
    envelope_json: Optional[str] = typer.Option(
        None,
        "--envelope-json",
        help="Envelope JSON string, @file, or '-' for stdin",
    ),
    envelope_file: Optional[Path] = typer.Option(
        None,
        "--envelope-file",
        help="Path to envelope JSON file",
    ),
    event_id: Optional[str] = typer.Option(
        None, "--event-id", help="Override event_id for the envelope"
    ),
    correlation_id: List[str] = typer.Option(
        [], "--correlation-id", "-c", help="Parent event id(s) for correlation"
    ),
    source_type: TriggerType = typer.Option(
        TriggerType.MANUAL, "--source-type", help="Event source type"
    ),
    source_app: str = typer.Option(
        "bloodbank-cli", "--source-app", help="Event source application"
    ),
    source_host: Optional[str] = typer.Option(
        None, "--source-host", help="Event source host (defaults to hostname)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print payload without publishing"
    ),
    skip_validation: bool = typer.Option(
        False, "--skip-validation", help="Skip schema validation (not recommended)"
    ),
    strict_validation: bool = typer.Option(
        True, "--strict-validation/--permissive-validation", help="Fail if schema not found"
    ),
):
    """
    Publish an event to the event bus.

    Examples:
        bb publish fireflies.transcript.ready --payload-file payload.json
        bb publish fireflies.transcript.ready --envelope-file envelope.json
        bb publish fireflies.transcript.ready --json '{"id":"..."}'
        bb publish fireflies.transcript.ready --json - < payload.json
    """
    event_info = get_event_by_name(event_name)

    # If event not found in registry, treat it as an ad-hoc event type
    # This allows publishing events defined only in HolyFields schemas
    if not event_info:
        console.print(f"[yellow]Event '{event_name}' not found in registry.[/yellow]")
        console.print(f"[dim]Treating as ad-hoc event type (routing key: {event_name})[/dim]")

        # For ad-hoc events, use basic payload structure
        event_class = None
        routing_key = event_name
        is_ad_hoc = True
    else:
        event_class = event_info["class"]
        routing_key = event_info["routing_key"]
        is_ad_hoc = False

    using_envelope = envelope_json is not None or envelope_file is not None
    using_payload = payload_json is not None or payload_file is not None

    if sum([mock, using_payload, using_envelope]) == 0:
        console.print(
            "[red]Error: provide --mock, --json/--payload-file, or --envelope-json/--envelope-file[/red]"
        )
        raise typer.Exit(1)

    if sum([mock, using_payload, using_envelope]) > 1:
        console.print(
            "[red]Error: choose only one of --mock, --json/--payload-file, or --envelope-json/--envelope-file[/red]"
        )
        raise typer.Exit(1)

    if using_envelope:
        if envelope_json:
            envelope_data = _load_json_arg(envelope_json)
        else:
            envelope_data = json.loads(envelope_file.read_text())

        event_type = envelope_data.get("event_type") or routing_key
        payload_data = envelope_data.get("payload") or {}

        # Create payload instance (if event class available)
        if event_class:
            try:
                payload_instance = event_class(**payload_data)
            except Exception as e:
                console.print(f"[red]Error creating event payload: {e}[/red]")
                raise typer.Exit(1)
        else:
            # Ad-hoc event: use raw dict payload
            payload_instance = payload_data

        source_data = envelope_data.get("source") or {
            "host": source_host or socket.gethostname(),
            "type": source_type,
            "app": source_app,
        }

        correlation_ids: List[UUID] = []
        for c in envelope_data.get("correlation_ids", []):
            try:
                correlation_ids.append(UUID(str(c)))
            except ValueError:
                console.print(f"[yellow]Skipping invalid correlation id: {c}[/yellow]")

        timestamp = envelope_data.get("timestamp") or datetime.now(timezone.utc)

        envelope = EventEnvelope(
            event_id=UUID(str(envelope_data["event_id"])) if envelope_data.get("event_id") else (UUID(event_id) if event_id else uuid4()),
            event_type=event_type,
            timestamp=timestamp,
            version=envelope_data.get("version", "1.0.0"),
            source=Source.model_validate(source_data),
            correlation_ids=correlation_ids,
            agent_context=envelope_data.get("agent_context"),
            payload=payload_instance.model_dump() if hasattr(payload_instance, 'model_dump') else payload_instance,
        )
        routing_key = event_type
    else:
        if mock:
            if is_ad_hoc:
                console.print(f"[red]Error: --mock not supported for ad-hoc events[/red]")
                raise typer.Exit(1)
            payload_data = load_mock_data(event_info)
            if not payload_data:
                console.print(f"[red]Error: No mock data found for {event_info['name']}[/red]")
                raise typer.Exit(1)
        elif payload_json:
            payload_data = _load_json_arg(payload_json)
        else:
            payload_data = json.loads(payload_file.read_text())

        # Create payload instance (if event class available)
        if event_class:
            try:
                payload_instance = event_class(**payload_data)
            except Exception as e:
                console.print(f"[red]Error creating event payload: {e}[/red]")
                raise typer.Exit(1)
        else:
            # Ad-hoc event: use raw dict payload
            payload_instance = payload_data

        # Allow routing keys with templates like artifact.{action}
        if "{" in routing_key:
            try:
                routing_key = routing_key.format(**payload_data)
            except KeyError as e:
                console.print(f"[red]Missing key for routing_key format: {e}[/red]")
                raise typer.Exit(1)

        source = Source(
            host=source_host or socket.gethostname(),
            type=source_type,
            app=source_app,
        )

        correlation_ids = _parse_uuid_list(correlation_id)

        # Create envelope (handle both Pydantic models and dicts)
        if is_ad_hoc or not hasattr(payload_instance, 'model_dump'):
            # Manual envelope creation for ad-hoc events
            envelope = EventEnvelope(
                event_id=UUID(event_id) if event_id else uuid4(),
                event_type=routing_key,
                timestamp=datetime.now(timezone.utc),
                version="1.0.0",
                source=source,
                correlation_ids=correlation_ids,
                agent_context=None,
                payload=payload_instance if isinstance(payload_instance, dict) else payload_instance,
            )
        else:
            envelope = create_envelope(
                event_type=routing_key,
                payload=payload_instance,
                source=source,
                correlation_ids=correlation_ids,
                event_id=UUID(event_id) if event_id else None,
            )

    # Validate payload against HolyFields schema (unless skipped)
    if not skip_validation:
        console.print(f"\n[dim]Validating payload against HolyFields schema...[/dim]")
        validation_result = validate_event(
            event_type=routing_key,
            payload=envelope.payload,
            envelope=envelope.model_dump(),
            strict=strict_validation
        )

        if not validation_result.valid:
            console.print(f"\n[red]Schema validation failed:[/red]")
            for error in validation_result.errors:
                console.print(f"  [red]✗[/red] {error}")

            if strict_validation:
                console.print("\n[yellow]Tip: Use --skip-validation to bypass schema validation[/yellow]")
                console.print("[yellow]Tip: Use --permissive-validation to allow missing schemas[/yellow]")
                raise typer.Exit(1)
            else:
                console.print("\n[yellow]Continuing with permissive validation...[/yellow]")
        else:
            console.print(f"[green]✓ Schema validation passed[/green] ({validation_result.schema_path})")

    payload_json_out = envelope.model_dump_json(indent=2)

    if dry_run:
        console.print("\n[bold]Dry run - would publish:[/bold]")
        syntax = Syntax(payload_json_out, "json", theme="monokai", line_numbers=True)
        console.print(syntax)
        return

    try:
        asyncio.run(_publish_envelope(routing_key, envelope))
        console.print(
            f"[green]✓ Published {routing_key} (event_id: {envelope.event_id})[/green]"
        )
    except asyncio.TimeoutError as e:
        console.print(f"[red]Timeout error: {e}[/red]")
        console.print("[yellow]Tip: Check RabbitMQ connectivity or increase RABBIT_PUBLISH_TIMEOUT[/yellow]")
        raise typer.Exit(1)
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
