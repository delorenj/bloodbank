#!/usr/bin/env python3
"""
Event Browser Widget for Bloodbank TUI.

Provides a tree view for navigating events discovered by the EventRegistry.
"""

from textual.widgets import Tree
from textual.message import Message
from typing import Dict
from event_producers.events.registry import EventRegistry


class EventBrowser(Tree):
    """Tree widget for browsing events by domain."""

    class EventSelected(Message):
        """Message emitted when an event is selected."""

        def __init__(self, event_type: str) -> None:
            self.event_type = event_type
            super().__init__()

    def __init__(self, registry: EventRegistry, **kwargs):
        super().__init__("Events", **kwargs)
        self.registry = registry
        self.event_nodes: Dict[str, Tree.Node] = {}

    def on_mount(self) -> None:
        """Populate the tree with events when mounted."""
        self._populate_tree()
        self.root.expand()

    def _populate_tree(self) -> None:
        """Populate the tree with domains and events."""
        domains = self.registry.list_domains()

        for domain_name in sorted(domains):
            # Create domain node
            domain_node = self.root.add(f"📁 {domain_name}", data=domain_name)

            # Get events for this domain
            events = self.registry.list_domain_events(domain_name)

            for event_type in sorted(events):
                # Create event node
                event_label = f"📄 {event_type.split('.')[-1]}"
                event_node = domain_node.add(event_label, data=event_type)
                self.event_nodes[event_type] = event_node

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle node selection."""
        if event.node.data and isinstance(event.node.data, str):
            # Check if this is an event (has dots in the name)
            if "." in event.node.data:
                self.post_message(self.EventSelected(event.node.data))
