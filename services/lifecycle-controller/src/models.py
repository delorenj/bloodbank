"""Compatibility entrypoint; canonical project-health implementation is in Krebs.

Existing database tables and data are unchanged. Install krebs-execution when
running this component standalone.
"""
from pathlib import Path
import sys
_checkout = Path(__file__).resolve().parents[4] / "krebs" / "src"
if _checkout.is_dir():
    sys.path.insert(0, str(_checkout))
import importlib
_canonical = importlib.import_module("krebs.project_health.models")
globals().update({name: value for name, value in vars(_canonical).items() if not name.startswith("__")})
