"""
Event registry for managing event types and their payload schemas.

This module provides a centralized registry pattern for discovering and validating
event types across the Bloodbank event system. It auto-discovers event payload
classes from domain modules and provides type-safe access to event metadata.

Usage:
    >>> from event_producers.events.registry import get_registry
    >>> registry = get_registry()
    >>> registry.auto_discover_domains()
    >>> payload_type = registry.get_payload_type("fireflies.transcript.ready")
    >>> print(payload_type)
    <class 'FirefliesTranscriptReadyPayload'>
"""

import importlib
import inspect
import os
from typing import Dict, List, Optional, Type, Any
from pathlib import Path
from pydantic import BaseModel



class EventDomain:
    """
    Represents a domain of related events (e.g., fireflies, agent/thread, github).

    A domain groups together logically related events and their payload types.
    Each domain corresponds to a module in event_producers.events.domains that
    contains payload class definitions and a ROUTING_KEYS dictionary.

    Attributes:
        name: Domain name (e.g., "fireflies", "agent/thread")
        module_name: Full Python module path (e.g., "event_producers.events.domains.fireflies")
        payload_types: Mapping of event type (routing key) to payload class

    Example:
        >>> domain = EventDomain("fireflies", "event_producers.events.domains.fireflies")
        >>> domain.add_event_type("fireflies.transcript.ready", FirefliesTranscriptReadyPayload)
        >>> domain.list_events()
        ['fireflies.transcript.ready', 'fireflies.transcript.upload', ...]
    """

    def __init__(self, name: str, module_name: str):
        """
        Initialize an event domain.

        Args:
            name: Short domain name (e.g., "fireflies", "agent/thread")
            module_name: Full Python module path containing the domain's events
        """
        self.name = name
        self.module_name = module_name
        self.payload_types: Dict[str, Type[BaseModel]] = {}

    def add_event_type(self, event_type: str, payload_class: Type[BaseModel]) -> None:
        """
        Register an event type within this domain.

        Args:
            event_type: Routing key for the event (e.g., "fireflies.transcript.ready")
            payload_class: Pydantic model class for the event payload

        Raises:
            ValueError: If event_type is already registered with a different payload class
        """
        if event_type in self.payload_types:
            existing = self.payload_types[event_type]
            if existing != payload_class:
                raise ValueError(
                    f"Event type '{event_type}' already registered in domain '{self.name}' "
                    f"with payload class {existing.__name__}, cannot re-register with {payload_class.__name__}"
                )
        self.payload_types[event_type] = payload_class

    def get_payload_type(self, event_type: str) -> Optional[Type[BaseModel]]:
        """
        Get the payload class for a specific event type.

        Args:
            event_type: Routing key for the event

        Returns:
            Payload class if found, None otherwise
        """
        return self.payload_types.get(event_type)

    def list_events(self) -> List[str]:
        """
        List all event types registered in this domain.

        Returns:
            Sorted list of event type routing keys
        """
        return sorted(self.payload_types.keys())

    def __repr__(self) -> str:
        return f"EventDomain(name='{self.name}', events={len(self.payload_types)})"


class EventRegistry:
    """
    Centralized registry for all event types and their payload schemas.

    The registry auto-discovers event payload classes from domain modules and
    provides type-safe access to event metadata. It scans modules in
    event_producers.events.domains recursively to find:
    - Classes ending with "Payload" that inherit from BaseModel
    - ROUTING_KEYS dictionary mapping class names to routing keys

    This enables runtime validation, schema generation, and type introspection
    for the event-driven architecture.

    Attributes:
        domains: Mapping of domain name to EventDomain instance

    Example:
        >>> registry = EventRegistry()
        >>> registry.auto_discover_domains()
        >>> registry.list_domains()
        ['agent/thread', 'fireflies', 'github']
        >>> payload_type = registry.get_payload_type("fireflies.transcript.ready")
        >>> schema = registry.get_schema("fireflies.transcript.ready")
    """

    def __init__(self):
        """Initialize an empty event registry."""
        self.domains: Dict[str, EventDomain] = {}

    def register_domain(self, domain: EventDomain) -> None:
        """
        Register a new event domain.

        Args:
            domain: EventDomain instance to register

        Raises:
            ValueError: If domain name is already registered
        """
        if domain.name in self.domains:
            raise ValueError(f"Domain '{domain.name}' is already registered")
        self.domains[domain.name] = domain

    def register(
        self,
        event_type: str,
        payload_class: Type[BaseModel],
        domain_name: Optional[str] = None,
    ) -> None:
        """
        Register an event type with its payload class.

        If domain_name is not provided, it will be inferred from the event_type
        (first component before the first dot, e.g., "fireflies" from "fireflies.transcript.ready").

        Args:
            event_type: Routing key for the event (e.g., "fireflies.transcript.ready")
            payload_class: Pydantic model class for the event payload
            domain_name: Optional domain name (inferred from event_type if not provided)

        Example:
            >>> registry.register("fireflies.transcript.ready", FirefliesTranscriptReadyPayload)
            >>> registry.register("custom.event", CustomPayload, domain_name="custom")
        """
        # Infer domain name from event_type if not provided
        if domain_name is None:
            parts = event_type.split(".")
            if len(parts) < 2:
                raise ValueError(
                    f"Cannot infer domain from event_type '{event_type}'. "
                    "Expected format: 'domain.category.action'"
                )
            domain_name = parts[0]

        # Create domain if it doesn't exist
        if domain_name not in self.domains:
            module_name = (
                f"event_producers.events.domains.{domain_name.replace('/', '.')}"
            )
            domain = EventDomain(domain_name, module_name)
            self.domains[domain_name] = domain

        # Register event type in domain
        self.domains[domain_name].add_event_type(event_type, payload_class)

    def get_payload_type(self, event_type: str) -> Optional[Type[BaseModel]]:
        """
        Get the payload class for a specific event type.

        Searches all registered domains for the event type.

        Args:
            event_type: Routing key for the event

        Returns:
            Payload class if found, None otherwise

        Example:
            >>> payload_type = registry.get_payload_type("fireflies.transcript.ready")
            >>> if payload_type:
            ...     payload = payload_type(id="123", title="Meeting", ...)
        """
        for domain in self.domains.values():
            payload_type = domain.get_payload_type(event_type)
            if payload_type is not None:
                return payload_type
        return None

    def is_valid_event_type(self, event_type: str) -> bool:
        """
        Check if an event type is registered.

        Args:
            event_type: Routing key to validate

        Returns:
            True if event type is registered, False otherwise

        Example:
            >>> registry.is_valid_event_type("fireflies.transcript.ready")
            True
            >>> registry.is_valid_event_type("invalid.event.type")
            False
        """
        return self.get_payload_type(event_type) is not None

    def list_domains(self) -> List[str]:
        """
        List all registered domain names.

        Returns:
            Sorted list of domain names

        Example:
            >>> registry.list_domains()
            ['agent/thread', 'fireflies', 'github']
        """
        return sorted(self.domains.keys())

    def list_domain_events(self, domain_name: str) -> List[str]:
        """
        List all event types in a specific domain.

        Args:
            domain_name: Name of the domain

        Returns:
            Sorted list of event type routing keys in that domain

        Raises:
            KeyError: If domain_name is not registered

        Example:
            >>> registry.list_domain_events("fireflies")
            ['fireflies.transcript.failed', 'fireflies.transcript.processed', ...]
        """
        if domain_name not in self.domains:
            raise KeyError(f"Domain '{domain_name}' not found in registry")
        return self.domains[domain_name].list_events()

    def get_schema(self, event_type: str) -> Optional[Dict[str, Any]]:
        """
        Get the JSON schema for an event type's payload.

        Returns the Pydantic model schema which can be used for validation,
        documentation generation, or API specifications.

        Args:
            event_type: Routing key for the event

        Returns:
            JSON schema dict if event type is found, None otherwise

        Example:
            >>> schema = registry.get_schema("fireflies.transcript.ready")
            >>> print(schema['properties']['title'])
            {'type': 'string', 'title': 'Title'}
        """
        payload_type = self.get_payload_type(event_type)
        if payload_type is None:
            return None
        return payload_type.model_json_schema()

    def auto_discover_domains(self) -> None:
        """
        Automatically discover and register all event domains.

        Scans the event_producers.events.domains package recursively for domain modules.
        Supports nested domain structures like:
        - domains/fireflies.py -> fireflies.*
        - domains/agent/thread.py -> agent.thread.*

        For each module, it:
        1. Imports the module
        2. Reads the ROUTING_KEYS dictionary to identify event type mappings
        3. Finds all classes referenced in ROUTING_KEYS that inherit from BaseModel
        4. Registers each event type with its payload class

        The ROUTING_KEYS dictionary maps class names to routing keys:
            ROUTING_KEYS = {
                "AgentThreadPrompt": "agent.thread.prompt",
                "FirefliesTranscriptReadyPayload": "fireflies.transcript.ready",
            }

        This method should be called once at application startup to populate
        the registry with all available event types.

        Example:
            >>> registry = EventRegistry()
            >>> registry.auto_discover_domains()
            >>> len(registry.list_domains())
            3

        Note:
            This method will skip modules that don't have a ROUTING_KEYS dictionary
            or that fail to import. Errors are printed to stdout but don't stop
            discovery, allowing partial registration in development environments.
        """
        try:
            # Import the domains package
            import event_producers.events.domains as domains_package

            # Get the package path
            package_path = Path(domains_package.__file__).parent

            # Discover modules recursively
            for module_info in self._discover_modules_recursive(package_path):
                module_name = module_info.name
                full_module_name = module_info.full_name

                # Skip __init__ and private modules
                if module_name.startswith("_"):
                    continue

                try:
                    # Import the domain module
                    module = importlib.import_module(full_module_name)

                    # Get ROUTING_KEYS dictionary
                    routing_keys = getattr(module, "ROUTING_KEYS", None)
                    if routing_keys is None:
                        # Skip modules without ROUTING_KEYS
                        continue

                    # Create domain name from module path
                    # For nested modules like agent.thread, domain becomes "agent/thread"
                    relative_path = full_module_name.replace(
                        "event_producers.events.domains.", ""
                    )
                    domain_name = relative_path.replace(".", "/")

                    # Create domain
                    domain = EventDomain(domain_name, full_module_name)

                    # Find all payload classes in the module
                    # We look for classes that are referenced in ROUTING_KEYS
                    # and inherit from BaseModel
                    payload_classes = {}
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        # Check if it's a class defined in this module that inherits from BaseModel
                        # and is referenced in ROUTING_KEYS
                        if (
                            issubclass(obj, BaseModel)
                            and obj.__module__ == full_module_name
                            and name in routing_keys
                        ):
                            payload_classes[name] = obj

                    # Register each event type from ROUTING_KEYS
                    for class_name, routing_key in routing_keys.items():
                        payload_class = payload_classes.get(class_name)
                        if payload_class is not None:
                            domain.add_event_type(routing_key, payload_class)

                    # Register the domain if it has any events
                    if domain.list_events():
                        self.register_domain(domain)

                except Exception as e:
                    # Log error but continue discovery
                    # In production, you might want to use proper logging
                    print(f"Warning: Failed to discover domain '{module_name}': {e}")
                    continue

        except ImportError as e:
            print(f"Warning: Failed to import domains package: {e}")

    def _discover_modules_recursive(self, package_path: Path):
        """
        Recursively discover all Python modules in the domains package.

        Returns a list of ModuleInfo objects with name and full_name attributes.
        """
<<<<<<< HEAD

class ModuleInfo:
    def __init__(self, name: str, full_name: str):
        self.name = name
        self.full_name = full_name
        modules = []

        for root, dirs, files in os.walk(package_path):
            # Skip __pycache__ directories
            if "__pycache__" in root:
                continue

            for file in files:
                if file.endswith(".py") and not file.startswith("__"):
                    # Get the relative path from package_path
                    rel_path = Path(root).relative_to(package_path)
                    module_name = file[:-3]  # Remove .py extension

                    # Build the module path components
                    if rel_path == Path("."):
                        # Direct child of domains package
                        full_module_name = (
                            f"event_producers.events.domains.{module_name}"
                        )
                    else:
                        # Nested module (e.g., agent/thread)
                        path_parts = list(rel_path.parts) + [module_name]
                        full_module_name = "event_producers.events.domains." + ".".join(
                            path_parts
                        )

                    modules.append(ModuleInfo(module_name, full_module_name))

        return modules

    def __repr__(self) -> str:
        total_events = sum(
            len(domain.payload_types) for domain in self.domains.values()
        )
        return f"EventRegistry(domains={len(self.domains)}, events={total_events})"


# ============================================================================
# Global Registry Singleton
# ============================================================================

_global_registry: Optional[EventRegistry] = None


def get_registry() -> EventRegistry:
    """
    Get the global event registry singleton.

    The registry is lazily initialized on first access and auto-discovers
    all event domains on creation.

    Returns:
        Global EventRegistry instance

    Example:
        >>> registry = get_registry()
        >>> registry.is_valid_event_type("fireflies.transcript.ready")
        True

    Note:
        The registry is initialized once per process. In testing environments,
        you may want to reset the registry between tests by setting
        _global_registry to None.
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = EventRegistry()
        _global_registry.auto_discover_domains()
    return _global_registry


def register_event(
    event_type: str, payload_class: Type[BaseModel], domain_name: Optional[str] = None
) -> None:
    """
    Helper function to register an event type with the global registry.

    This is a convenience function that delegates to the global registry's
    register method. Use this for manually registering custom event types
    that aren't auto-discovered.

    Args:
        event_type: Routing key for the event (e.g., "custom.event.created")
        payload_class: Pydantic model class for the event payload
        domain_name: Optional domain name (inferred from event_type if not provided)

    Example:
        >>> class CustomEventPayload(BaseModel):
        ...     message: str
        >>> register_event("custom.event.created", CustomEventPayload)
        >>> registry = get_registry()
        >>> registry.is_valid_event_type("custom.event.created")
        True
    """
    registry = get_registry()
    registry.register(event_type, payload_class, domain_name)
