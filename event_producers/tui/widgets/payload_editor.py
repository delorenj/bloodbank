#!/usr/bin/env python3"""
Payload Editor Widget for Bloodbank TUI.

Provides JSON editing capabilities for event payloads with validation.
"""

from textual.widgets import TextArea
from textual.message import Message
from textual import log
from typing import Optional, Dict, Any, Tuple
import json
import pydantic
from event_producers.events.registry import EventRegistry


class PayloadEditor(TextArea):
    """Widget for editing event payloads with validation."""
    
    class PayloadChanged(Message):
        """Message emitted when payload is modified."""
        def __init__(self, payload: Dict[str, Any]) -> None:
            self.payload = payload
            super().__init__()
    
    def __init__(self, **kwargs):
        super().__init__("Select an event to edit payload", **kwargs)
        self.current_event_type: Optional[str] = None
        self.current_schema: Optional[Dict[str, Any]] = None
        self.registry: Optional[EventRegistry] = None
        
    def load_event(self, event_type: str, schema: Optional[Dict[str, Any]]) -> None:
        """Load an event for editing."""
        self.current_event_type = event_type
        self.current_schema = schema
        
        # Try to load mock data first
        mock_data = self._load_mock_file()
        if mock_data:
            payload_json = json.dumps(mock_data, indent=2)
        else:
            # Generate sample payload from schema
            payload_json = self._generate_sample_payload(schema)
            
        self.text = payload_json
        self._notify_payload_changed()
        
    def _generate_sample_payload(self, schema: Optional[Dict[str, Any]]) -> str:
        """Generate a sample payload from schema."""
        if not schema or 'properties' not in schema:
            return '{\n  \"sample\": \"value\"\n}'
            
        sample = {}
        for prop_name, prop_info in schema['properties'].items():
            prop_type = prop_info.get('type', 'string')
            if prop_type == 'string':
                sample[prop_name] = f"sample_{prop_name}"
            elif prop_type == 'number':
                sample[prop_name] = 0
            elif prop_type == 'boolean':
                sample[prop_name] = False
            elif prop_type == 'array':
                sample[prop_name] = []
            elif prop_type == 'object':
                sample[prop_name] = {}
            else:
                sample[prop_name] = None
                
        return json.dumps(sample, indent=2)
        
    def _load_mock_file(self) -> Optional[Dict[str, Any]]:
        """Load mock data from file if it exists."""
        if not self.current_event_type:
            return None
            
        try:
            # Convert event type to filename (e.g., agent.thread.prompt -> agent_thread_prompt.json)
            filename = self.current_event_type.replace('.', '_') + '_Mock.json'
            with open(filename, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
            
    def save_mock_file(self) -> None:
        """Save current payload as mock data."""
        if not self.current_event_type:
            return
            
        try:
            payload = self._get_current_payload()
            if not payload:
                return
                
            filename = self.current_event_type.replace('.', '_') + '_Mock.json'
            with open(filename, 'w') as f:
                json.dump(payload, f, indent=2)
                
        except Exception as e:
            log(f"Error saving mock file: {e}")
            
    def load_mock_file(self) -> bool:
        """Load mock data from file and update editor."""
        mock_data = self._load_mock_file()
        if mock_data:
            self.text = json.dumps(mock_data, indent=2)
            self._notify_payload_changed()
            return True
        return False
        
    def validate_payload(self) -> Tuple[bool, List[str]]:
        """Validate current payload against schema."""
        if not self.current_event_type or not self.registry:
            return False, ["No event loaded"]
            
        try:
            payload = self._get_current_payload()
            if not payload:
                return False, ["Invalid JSON"]
                
            # Get payload type and validate
            payload_type = self.registry.get_payload_type(self.current_event_type)
            if not payload_type:
                return False, ["No payload type found"]
                
            # Validate using Pydantic
            payload_type(**payload)
            return True, []
            
        except pydantic.ValidationError as e:
            errors = [str(error) for error in e.errors()]
            return False, errors
        except Exception as e:
            return False, [f"Validation error: {str(e)}"]
            
    def _get_current_payload(self) -> Optional[Dict[str, Any]]:
        """Parse current text as JSON."""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None
            
    def _notify_payload_changed(self) -> None:
        """Notify listeners that payload has changed."""
        payload = self._get_current_payload()
        if payload:
            self.post_message(self.PayloadChanged(payload))
            
    def on_changed(self) -> None:
        """Handle text changes."""
        self._notify_payload_changed()
