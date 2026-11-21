#!/usr/bin/env python3
"""Regression guard for tracked Bloodbank Plane bindings, independent of PM runtime state."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PROJECT_ID = "10d06f8d-c110-4ce5-beaa-0914534b090a"
OBSOLETE_PROJECT_ID = "b089fbf5-52c3-49c4-8f4b-d390c671f6b4"
MANIFEST_PATH = ".project.json"
ROLE_PATH = "agents/hermes/pm/role.yaml"


def verify(root: Path) -> None:
    manifest = json.loads((root / MANIFEST_PATH).read_text())
    role = yaml.safe_load((root / ROLE_PATH).read_text())
    board = manifest["ticket_provider"]
    plane = role["plane"]
    assert board["type"] == "plane"
    assert board["workspace"] == plane["workspace"] == "33god"
    assert board["board_id"] == plane["project_id"] == EXPECTED_PROJECT_ID
    assert board["identifier"] == plane["identifier"] == "BB"
    assert OBSOLETE_PROJECT_ID not in json.dumps({"manifest": manifest, "role": role})


def check_negative_bindings() -> None:
    # Copy only tracked authority files: no operator-local marker or shared checkout.
    with tempfile.TemporaryDirectory(prefix="bb61-plane-binding-") as temporary:
        root = Path(temporary)
        for name in (MANIFEST_PATH, ROLE_PATH):
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / name).read_bytes())
        verify(root)
        for file, binding in ((MANIFEST_PATH, "board_id"), (ROLE_PATH, "project_id")):
            path = root / file
            original = path.read_text()
            obj = json.loads(original) if file == MANIFEST_PATH else yaml.safe_load(original)
            section = obj["ticket_provider"] if file == MANIFEST_PATH else obj["plane"]
            for wrong in (OBSOLETE_PROJECT_ID, "00000000-0000-4000-8000-000000000000"):
                section[binding] = wrong
                path.write_text(json.dumps(obj) if file == MANIFEST_PATH else yaml.safe_dump(obj))
                try:
                    verify(root)
                except AssertionError:
                    pass
                else:
                    raise AssertionError(f"{file} accepted wrong {binding}: {wrong}")
            path.write_text(original)


if __name__ == "__main__":
    verify(ROOT)
    check_negative_bindings()
    print("smoketest-plane-binding: PASS (tracked bindings and isolated mismatches)")
