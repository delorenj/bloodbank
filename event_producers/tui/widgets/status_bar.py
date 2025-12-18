#!/usr/bin/env python3"""
Status Bar Widget for Bloodbank TUI.

Provides status updates and feedback for user actions.
"""

from textual.widgets import Static
from typing import Optional


class StatusBar(Static):
    """Widget for displaying status messages."""
    
    def __init__(self, **kwargs):
        super().__init__("Ready", **kwargs)
        self.current_message: str = "Ready"
        
    def set_message(self, message: str) -> None:
        """Update the status message."""
        self.current_message = message
        self.update(f"📍 {message}")
        
    def clear(self) -> None:
        """Clear the status message."""
        self.set_message("Ready")
