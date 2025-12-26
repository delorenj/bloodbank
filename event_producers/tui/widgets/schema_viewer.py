#!/usr/bin/env python3
"""
Schema Viewer Widget for Bloodbank TUI.

Displays Pydantic schema information for selected events.
"""

from textual.widgets import Static
from textual import log
from typing import Optional, Dict, Any
import json


class SchemaViewer(Static):
    """Widget for displaying event schemas."""

    def __init__(self, **kwargs):
        super().__init__("Select an event to view schema", **kwargs)
        self.current_event_type: Optional[str] = None
        self.current_schema: Optional[Dict[str, Any]] = None

    def show_schema(self, event_type: str, schema: Optional[Dict[str, Any]]) -> None:
        """Display schema for an event type."""
        self.current_event_type = event_type
        self.current_schema = schema

        if not schema:
            self.update(f"No schema available for {event_type}")
            return

        # Format schema as JSON
        try:
            schema_json = json.dumps(schema, indent=2)
            schema_text = f"""📋 Schema: {event_type}

```json
{schema_json}
```"""
            self.update(schema_text)
        except Exception as e:
            log(f"Error formatting schema: {e}")
            self.update(f"Error displaying schema for {event_type}")
