#!/usr/bin/env python
"""Extract and analyze the ExtractionOutput JSON schema."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from corpdisc.contracts.agent_io import ExtractionOutput


def count_nesting(obj: dict | list, current: int = 0) -> int:
    """Count maximum nesting depth."""
    if isinstance(obj, dict):
        if not obj:
            return current
        return max(count_nesting(v, current + 1) for v in obj.values()) if obj else current
    if isinstance(obj, list):
        if not obj:
            return current
        return max(count_nesting(item, current + 1) for item in obj) if obj else current
    return current


def analyze_schema(schema: dict) -> dict:
    """Analyze schema characteristics."""
    stats = {
        "byte_size": 0,
        "nesting_depth": 0,
        "properties_count": 0,
        "arrays_count": 0,
        "enums_count": 0,
        "ref_count": 0,
        "anyof_count": 0,
        "oneof_count": 0,
        "allof_count": 0,
        "nullable_fields": [],
        "max_string_length": 0,
        "has_defs": False,
        "defs_keys": [],
    }

    def traverse(obj: dict | list | Any, path: str = ""):
        if isinstance(obj, dict):
            stats["properties_count"] += len(obj)

            if "$defs" in obj:
                stats["has_defs"] = True
                stats["defs_keys"] = list(obj["$defs"].keys())

            for k, v in obj.items():
                if k == "$ref":
                    stats["ref_count"] += 1
                elif k == "anyOf":
                    stats["anyof_count"] += 1
                elif k == "oneOf":
                    stats["oneof_count"] += 1
                elif k == "allOf":
                    stats["allof_count"] += 1
                elif k == "enum":
                    stats["enums_count"] += 1
                elif k == "type" and v == "array":
                    stats["arrays_count"] += 1
                elif k == "nullable" and v is True:
                    stats["nullable_fields"].append(path or "(root)")

                if k == "maxLength" and isinstance(v, int):
                    stats["max_string_length"] = max(stats["max_string_length"], v)

                traverse(v, f"{path}.{k}" if path else k)

        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                traverse(item, f"{path}[{i}]")

    traverse(schema)
    stats["byte_size"] = len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    stats["nesting_depth"] = count_nesting(schema)

    return stats


if __name__ == "__main__":
    schema = ExtractionOutput.model_json_schema()

    # Save full schema
    out_dir = pathlib.Path(__file__).resolve().parent.parent / "diagnostics"
    out_dir.mkdir(exist_ok=True)

    schema_path = out_dir / "extraction_output_schema.json"
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)

    # Analyze and print
    stats = analyze_schema(schema)

    print("=" * 80)
    print("EXTRACTIONOUTPUT JSON SCHEMA CHARACTERISTICS")
    print("=" * 80)
    print(f"\nSchema saved to: {schema_path}")
    print(f"\nByte size (compact JSON):        {stats['byte_size']:,} bytes")
    print(f"Nesting depth:                   {stats['nesting_depth']}")
    print(f"Total properties:                {stats['properties_count']}")
    print(f"Arrays:                          {stats['arrays_count']}")
    print(f"Enums:                           {stats['enums_count']}")
    print(f"$ref references:                 {stats['ref_count']}")
    print(f"anyOf constructs:                {stats['anyof_count']}")
    print(f"oneOf constructs:                {stats['oneof_count']}")
    print(f"allOf constructs:                {stats['allof_count']}")
    print(f"Nullable fields:                 {len(stats['nullable_fields'])}")
    print(f"Max string maxLength:            {stats['max_string_length']}")
    print(f"Has $defs:                       {stats['has_defs']}")
    if stats['has_defs']:
        print(f"$defs keys:                      {stats['defs_keys']}")

    print("\n" + "=" * 80)
    print("SCHEMA TOP-LEVEL KEYS")
    print("=" * 80)
    print(json.dumps(list(schema.keys()), indent=2))

    print("\n" + "=" * 80)
    print("ENTITIES ARRAY ITEM PROPERTIES")
    print("=" * 80)
    entities_schema = schema.get("properties", {}).get("entities", {})
    items = entities_schema.get("items", {})
    print(json.dumps(list(items.get("properties", {}).keys()), indent=2))

    print("\n" + "=" * 80)
    print("REJECTED ARRAY ITEM PROPERTIES")
    print("=" * 80)
    rejected_schema = schema.get("properties", {}).get("rejected", {})
    rejected_items = rejected_schema.get("items", {})
    print(json.dumps(list(rejected_items.get("properties", {}).keys()), indent=2))

    print("\n" + "=" * 80)
    print("MODEL_JSON_SCHEMA() (raw, first 5000 chars)")
    print("=" * 80)
    print(json.dumps(schema, indent=2)[:5000] + "\n...")
