"""event_producers package.

Keep top-level import lightweight so utility modules can be imported without
requiring the full FastStream runtime stack.
"""

try:
    from .consumer import broker, app, get_broker, get_app
except Exception:  # pragma: no cover
    broker = None
    app = None

    def get_broker():
        raise RuntimeError("FastStream consumer runtime is not available")

    def get_app():
        raise RuntimeError("FastStream consumer runtime is not available")


__all__ = ["broker", "app", "get_broker", "get_app"]
