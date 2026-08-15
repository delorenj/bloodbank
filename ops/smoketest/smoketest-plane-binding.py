#!/usr/bin/env python3
"""Regression guard for the Bloodbank Plane binding used by fleet fanout."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PROJECT_ID = "10d06f8d-c110-4ce5-beaa-0914534b090a"
OBSOLETE_PROJECT_ID = "b089fbf5-52c3-49c4-8f4b-d390c671f6b4"

manifest = json.loads((ROOT / ".project.json").read_text())
role = yaml.safe_load((ROOT / "agents/hermes/pm/role.yaml").read_text())
marker = (ROOT / "agents/hermes/pm/.scripts/.plane-project-id").read_text().strip()

assert manifest["ticket_provider"]["board_id"] == EXPECTED_PROJECT_ID
assert manifest["ticket_provider"]["identifier"] == "BB"
assert role["plane"]["project_id"] == EXPECTED_PROJECT_ID
assert role["plane"]["identifier"] == "BB"
assert marker == EXPECTED_PROJECT_ID
assert OBSOLETE_PROJECT_ID not in json.dumps({"manifest": manifest, "role": role, "marker": marker})
print("smoketest-plane-binding: PASS")
