"""Compatibility entrypoint; canonical project-health implementation is in Krebs.

Existing database tables and data are unchanged. Install krebs-execution when
running this component standalone.
"""
import importlib
_canonical = importlib.import_module("krebs.project_health.main")
globals().update({name: value for name, value in vars(_canonical).items() if not name.startswith("__")})

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
