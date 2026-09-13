"""Test-suite guards for agent-hooks.

The Agent State Machine writes to a SHARED, live Redis from inside
core.publisher.run(). Several suites here drive run() end-to-end with a fake
adapter, so without this they would each deposit a phantom agent row into the
real `asm:live` table that `asmctl` and Holocene read -- observed exactly once,
as a `cli=test` row, which is why this file exists.

Guarding here rather than in each suite means a NEW test cannot reintroduce the
leak by forgetting to mock something. Suites that genuinely exercise the ASM
opt back in explicitly (see tests/test_asm.py).

pytest loads this automatically; `unittest discover` does not, so the mise task
`asm:test` and `validate:agent-hooks` set the same variable in the environment.
"""
import os
import pytest

os.environ.setdefault("BLOODBANK_ASM", "false")
os.environ["BLOODBANK_ASM"] = "false"


@pytest.fixture(autouse=True)
def isolate_live_hook_hub(monkeypatch, tmp_path):
    """Publisher tests must not forward through an installed machine hub."""
    monkeypatch.setenv("BB_HOOK_OWNERSHIP", str(tmp_path / "absent-ownership.json"))
