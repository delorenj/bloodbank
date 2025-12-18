#!/usr/bin/env python3
"""
Main Bloodbank TUI Application.

A Textual-based terminal user interface for managing the Bloodbank event ecosystem.
Provides tree navigation of events, schema viewing, payload editing, and publishing.
"""

import os
from pathlib import Path
from sys import path

# Determine the project root dynamically to avoid hardcoded absolute paths.
# Prefer an environment variable if provided, otherwise derive it from this file's location.
project_root = os.getenv("BLOODBANK_ROOT")
if project_root is None:
    # Assuming this file is located at <project_root>/event_producers/tui/app.py
    project_root = str(Path(__file__).resolve().parents[3])
if project_root not in path:
    path.insert(0, project_root)
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Header, Footer, Button
from textual.reactive import reactive
from typing import Optional, Dict, Any
import httpx

from event_producers.events.registry import get_registry
from .widgets.event_browser import EventBrowser
from .widgets.schema_viewer import SchemaViewer
from .widgets.payload_editor import PayloadEditor
from .widgets.status_bar import StatusBar


class BloodbankTUI(App):
    """Main Bloodbank TUI application."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 3 3;
        grid-columns: 1fr 2fr 1fr;
        grid-rows: auto 1fr auto;
    }
    
    #header {
        column-span: 3;
        height: 3;
    }
    
    #event-browser {
        column-span: 1;
        row-span: 2;
    }
    
    #main-content {
        column-span: 2;
        row-span: 1;
    }
    
    #payload-editor {
        column-span: 1;
        row-span: 1;
    }
    
    #status-bar {
        column-span: 3;
        height: 3;
    }
    
    .container {
        border: solid $primary;
        padding: 1;
    }
    """

    TITLE = "Bloodbank Event Bus TUI"
    SUB_TITLE = "Zellij-native Event Management"

    selected_event_type: reactive[Optional[str]] = reactive(None)
    selected_schema: reactive[Optional[Dict[str, Any]]] = reactive(None)
    selected_payload: reactive[Optional[Dict[str, Any]]] = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.registry = get_registry()

    def compose(self) -> ComposeResult:
        """Create the main layout."""
        yield Header(id="header")

        with Container(id="event-browser"):
            yield EventBrowser(self.registry)

        with Container(id="main-content"):
            yield SchemaViewer()

        with Container(id="payload-editor"):
            yield PayloadEditor()

        with Horizontal(id="actions"):
            yield Button("Validate Payload", id="validate-btn")
            yield Button("Publish Event", id="publish-btn")
            yield Button("Load Mock", id="load-mock-btn")
            yield Button("Save Mock", id="save-mock-btn")

        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the application."""
        # Set registry reference for payload editor
        payload_editor = self.query_one(PayloadEditor)
        payload_editor.registry = self.registry
        self.query_one(EventBrowser).focus()

    def on_event_browser_event_selected(
        self, event: "EventBrowser.EventSelected"
    ) -> None:
        """Handle event selection from the browser."""
        self.selected_event_type = event.event_type

        # Get schema for the selected event
        schema = self.registry.get_schema(event.event_type)
        self.selected_schema = schema

        # Update schema viewer
        schema_viewer = self.query_one(SchemaViewer)
        schema_viewer.show_schema(event.event_type, schema)

        # Load mock data for the event
        payload_editor = self.query_one(PayloadEditor)
        payload_editor.load_event(event.event_type, schema)

        # Update status
        status_bar = self.query_one(StatusBar)
        status_bar.set_message(f"Selected: {event.event_type}")

    def on_payload_editor_payload_changed(
        self, event: "PayloadEditor.PayloadChanged"
    ) -> None:
        """Handle payload changes."""
        self.selected_payload = event.payload

        # Update status
        status_bar = self.query_one(StatusBar)
        status_bar.set_message(f"Payload updated: {len(event.payload)} fields")

    def on_button_pressed(self, event: "Button.Pressed") -> None:
        """Handle button presses."""
        if event.button.id == "validate-btn":
            self._validate_payload()
        elif event.button.id == "publish-btn":
            self._publish_event()
        elif event.button.id == "load-mock-btn":
            self._load_mock_data()
        elif event.button.id == "save-mock-btn":
            self._save_mock_data()

    def _validate_payload(self) -> None:
        """Validate the current payload against the schema."""
        if not self.selected_event_type or not self.selected_payload:
            self.notify("No event or payload to validate", severity="warning")
            return

        payload_editor = self.query_one(PayloadEditor)
        is_valid, errors = payload_editor.validate_payload()

        status_bar = self.query_one(StatusBar)
        if is_valid:
            status_bar.set_message("✅ Payload validation passed")
            self.notify("Payload is valid!", severity="success")
        else:
            status_bar.set_message(f"❌ Validation failed: {', '.join(errors)}")
            self.notify(f"Validation errors: {', '.join(errors)}", severity="error")

    async def _publish_event(self) -> None:
        """Publish the current event."""
        if not self.selected_event_type or not self.selected_payload:
            self.notify("No event or payload to publish", severity="warning")
            return

        # Validate first
        payload_editor = self.query_one(PayloadEditor)
        is_valid, errors = payload_editor.validate_payload()

        if not is_valid:
            self.notify(
                f"Cannot publish invalid payload: {', '.join(errors)}", severity="error"
            )
            return

        status_bar = self.query_one(StatusBar)
        status_bar.set_message("Publishing event...")

        try:
            # Create event envelope
            from event_producers.events.base import EventEnvelope, create_envelope, Source, TriggerType
            import socket

            source = Source(
                host=socket.gethostname(), trigger_type=TriggerType.MANUAL, app="bloodbank-tui"
            )

            envelope = create_envelope(
                event_type=self.selected_event_type,
                payload=self.selected_payload,
                source=source,
            )

            # Publish via HTTP
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://localhost:8682/events/custom", json=envelope.model_dump()
                )
                response.raise_for_status()

            status_bar = self.query_one(StatusBar)
            status_bar.set_message(f"✅ Published: {self.selected_event_type}")
            self.notify(
                f"Event published successfully: {envelope.event_id}", severity="success"
            )

        except Exception as e:
            status_bar.set_message(f"❌ Publish failed: {str(e)}")
            self.notify(f"Failed to publish event: {str(e)}", severity="error")

    def _load_mock_data(self) -> None:
        """Load mock data from file."""
        if not self.selected_event_type:
            self.notify("No event selected", severity="warning")
            return

        payload_editor = self.query_one(PayloadEditor)
        success = payload_editor.load_mock_file()

        if success:
            status_bar = self.query_one(StatusBar)
            status_bar.set_message("Mock data loaded")
            self.notify("Mock data loaded successfully", severity="success")
        else:
            self.notify("No mock data file found", severity="warning")

    def _save_mock_data(self) -> None:
        """Save current payload as mock data."""
        if not self.selected_event_type or not self.selected_payload:
            self.notify("No event or payload to save", severity="warning")
            return

        payload_editor = self.query_one(PayloadEditor)
        payload_editor.save_mock_file()

        status_bar = self.query_one(StatusBar)
        status_bar.set_message("Mock data saved")
        self.notify("Mock data saved successfully", severity="success")


def main():
    """Entry point for the TUI application."""
    app = BloodbankTUI()
    app.run()


if __name__ == "__main__":
    main()
