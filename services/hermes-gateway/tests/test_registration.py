from pathlib import Path
from types import SimpleNamespace

import tomllib
from bloodbank_hermes_gateway.adapter import register


def test_registers_standalone_platform_factory():
    calls = []
    hooks = {}
    ctx = SimpleNamespace(
        register_platform=lambda **kwargs: calls.append(kwargs),
        register_hook=lambda name, callback: hooks.setdefault(name, callback),
    )

    register(ctx)

    assert set(hooks) == {"pre_llm_call", "post_llm_call", "on_session_end"}
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


def test_adapter_declares_nobody_answers_approvals():
    # A Bloodbank command turn has no human reader: the gateway must reroute its approval
    # prompts to the profile's home channel instead of posting them into send(), which
    # delivers nothing (EARS-59: the prompt timed out unseen and the command was BLOCKED).
    from bloodbank_hermes_gateway.adapter import BloodbankAdapter

    assert BloodbankAdapter.answers_approvals is False
