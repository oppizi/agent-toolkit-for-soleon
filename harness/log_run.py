"""Append a run record to runs.jsonl — gated on the transcript AND the
generated spec artifact existing (the spec is the nondeterministic artifact;
without it a Friday-pass/Monday-fail can't be bisected into LLM-head drift vs
converter bug).

Usage: python3 harness/log_run.py --record '<json>'
The record must carry the floor fields plus transcript/spec_artifact paths.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = BET_ROOT / "runs.jsonl"

FLOOR_FIELDS = {
    "ts", "input_file", "arm", "turn_count", "ambiguities_surfaced",
    "schema_match", "corrections_needed", "soul_fidelity", "trap_scorecard",
    "duration_s", "transcript", "spec_artifact",
}


class HarnessError(Exception):
    pass


def append(record: dict) -> None:
    missing = FLOOR_FIELDS - record.keys()
    if missing:
        raise HarnessError(f"run record missing fields: {sorted(missing)}")
    for artifact_key in ("transcript", "spec_artifact"):
        path = BET_ROOT / record[artifact_key]
        if not path.is_file():
            raise HarnessError(
                f"refusing to append: {artifact_key} {path} does not exist — "
                "runs.jsonl entries must be backed by real artifacts"
            )
    if record["arm"] not in ("experiment", "control"):
        raise HarnessError(f"arm must be experiment|control, got {record['arm']!r}")
    with RUNS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--record", required=True)
    args = ap.parse_args()
    try:
        append(json.loads(args.record))
    except (json.JSONDecodeError, HarnessError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"appended to {RUNS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
