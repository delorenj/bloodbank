#!/usr/bin/env python3
"""Project artifact contract smoketest.

Proves, against the real schema tree, validator, discovery adapters,
sidecar pipeline and vault projector, that bloodbank.project.artifact.created
behaves as docs/event-naming.md (project) says: schema validation of a real
producer-shaped envelope, causation/content-ref/absolute-path invariants,
replay/revision idempotency, hold-not-guess attribution, vault mapping and
date-folder rules. This is the same suite as
services/artifact-sidecar/test_sidecar.py exposed as a mise task so
smoketest:schemas can chain it.

Run: python3 ops/smoketest/smoketest-project-artifact-contracts.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEST = REPO / "services" / "artifact-sidecar" / "test_sidecar.py"


def load_suite() -> unittest.TestSuite:
    sys.path.insert(0, str(TEST.parent))
    loader = unittest.TestLoader()
    spec = loader.loadTestsFromName("test_sidecar")
    return spec


def main() -> int:
    suite = load_suite()
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
