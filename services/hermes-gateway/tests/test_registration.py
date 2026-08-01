import tomllib
from pathlib import Path
from types import SimpleNamespace

from bloodbank_hermes_gateway.adapter import register


def test_registers_standalone_platform_factory():
    calls = []
    ctx = SimpleNamespace(register_platform=lambda **kwargs: calls.append(kwargs))

    register(ctx)

    assert len(calls) == 1
    entry = calls[0]
    assert entry["name"] == "bloodbank"
    assert entry["label"] == "Bloodbank"
    assert callable(entry["adapter_factory"])
    assert callable(entry["check_fn"])
    assert entry["allow_update_command"] is False


def test_entrypoint_loads_module_with_register_function():
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    entrypoint = project["project"]["entry-points"]["hermes_agent.plugins"]
    assert entrypoint["bloodbank-platform"] == "bloodbank_hermes_gateway"
