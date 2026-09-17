"""Compatibility entrypoint; canonical project-health implementation is in Krebs.

Existing database tables and data are unchanged. Install krebs-execution when
running this component standalone.
"""
import importlib
_canonical = importlib.import_module("krebs.project_health.worker")
globals().update({name: value for name, value in vars(_canonical).items() if not name.startswith("__")})
