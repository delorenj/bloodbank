#!/usr/bin/env python3
"""CAND-51-only, local, self-contained codegen view of the canonical contracts.

Some model generators drop oneOf beside allOf, or misorder inherited base
classes from modular references. Expand ONLY the known CloudEvents base and
primitive refs here. Unknown layouts/refs fail, never become untyped data.
The canonical files and runtime semantic validator remain authoritative.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/agent-hooks"))
from core.validate import CRON_CREATED_TYPE, CRON_DELETED_TYPE, load_event_schema  # noqa: E402


def codegen_schema(action: str, *, schema_version: int | None = None) -> dict:
    if action not in {"created", "deleted"}:
        raise ValueError("only CAND-51 created/deleted codegen views are supported")
    ce_type = CRON_CREATED_TYPE if action == "created" else CRON_DELETED_TYPE
    canonical = load_event_schema(ce_type)
    common = ROOT / "schemas/_common"
    base = json.loads((common / "cloudevent_base.v1.json").read_text())
    primitives = json.loads((common / "types.v1.json").read_text())["$defs"]

    def inline(node):
        if isinstance(node, list):
            return [inline(value) for value in node]
        if not isinstance(node, dict):
            return node
        current = copy.deepcopy(node)
        ref = current.pop("$ref", None)
        if ref is not None:
            file, separator, pointer = ref.partition("#/$defs/")
            if file not in {"types.v1.json", "../../_common/types.v1.json"} or not separator or pointer not in primitives:
                raise ValueError(f"unsupported CAND-51 codegen ref: {ref}")
            primitive = copy.deepcopy(primitives[pointer])
            conflicts = set(primitive) & set(current) - {"description", "title", "examples", "default"}
            if conflicts:
                raise ValueError(f"cannot flatten conflicting primitive constraints: {conflicts}")
            current = {**primitive, **current}
        return {key: inline(value) for key, value in current.items()}

    def flat(schema):
        result = copy.deepcopy(schema)
        if result.pop("allOf", None) != [{"$ref": "../../_common/cloudevent_base.v1.json"}]:
            raise ValueError("unsupported CAND-51 envelope inheritance")
        # All overwritten base constraints are redundant with the canonical
        # consts/patterns/closed payloads; the base's other fields stay intact.
        result["properties"] = {**copy.deepcopy(base["properties"]), **result["properties"]}
        result["required"] = sorted(set(base["required"]) | set(result["required"]))
        return inline(result)

    if action == "deleted":
        if schema_version is not None and (type(schema_version) is not int or schema_version != 1):
            raise ValueError("deleted supports only revision 1")
        return flat(canonical)
    versions = [
        branch.get("properties", {}).get("data", {}).get("properties", {}).get("schema_version", {}).get("const")
        for branch in canonical.get("oneOf", [])
    ]
    if versions != [1, 2]:
        raise ValueError("unsupported CAND-51 registration revision layout")
    if schema_version is not None:
        return flat(load_event_schema(ce_type, schema_version=schema_version))
    branches = []
    for version in (1, 2):
        branch = flat(load_event_schema(ce_type, schema_version=version))
        branch.pop("$id")
        branch.pop("$schema")
        branch["title"] = f"CandybarCreatedV{version}"
        branches.append(branch)
    return {
        "$schema": canonical["$schema"], "$id": canonical["$id"],
        "title": canonical["title"], "description": canonical["description"],
        "oneOf": branches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("created", "deleted"))
    parser.add_argument("--schema-version", type=int)
    args = parser.parse_args()
    print(json.dumps(codegen_schema(args.action, schema_version=args.schema_version), indent=2))


if __name__ == "__main__":
    main()
