"""Generate plugin/contract.json from agent-infra repo source TEXT.

Build-time tool (harness-only, never ships). Reads source as text because
`lambda/ui_admin/index.py` is un-importable outside a deployed env (reads
USER_POOL_ID at import time) and `scripts/agent_manager/registry.py` imports
boto3. The drift test (tests/test_contract_drift.py) re-runs this extraction
and diffs against the shipped contract.json — that is the CI guard that keeps
single-source-of-truth honest.

Usage: python3 harness/sync_contract.py [--repo-root PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

CONTRACT_VERSION = 1
ENGINE_VERSION = "3.2.4"

# Claude Code frontmatter `model:` aliases — never valid Bedrock catalog ids.
# An alias registers cleanly and silently breaks the agent at runtime, so the
# converter drops them (recorded in the report) instead of passing through.
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku", "inherit", "default"]

FRAMEWORK_GLOSSES = {
    "maverick": "Oppizi's in-house framework — the default choice; pick this unless told otherwise.",
    "nanobot": "Lightweight third-party framework for minimal single-purpose agents.",
    "openclaw": "Full-featured third-party framework matching the OpenClaw reference implementation.",
}


def _grep(text: str, pattern: str, source: str) -> str:
    m = re.search(pattern, text, re.M)
    if not m:
        raise SystemExit(f"sync_contract: pattern not found in {source}: {pattern}")
    return m.group(1)


def _set_literal(text: str, name: str, source: str) -> list[str]:
    raw = _grep(text, rf"^{name}\s*=\s*(\{{[^}}]*\}})", source)
    values = ast.literal_eval(raw)
    if not isinstance(values, set) or not all(isinstance(v, str) for v in values):
        raise SystemExit(f"sync_contract: {name} did not parse to a set of strings")
    return sorted(values)


def extract(repo_root: Path) -> dict:
    index_py = (repo_root / "lambda/ui_admin/index.py").read_text(encoding="utf-8")
    registry_py = (repo_root / "scripts/agent_manager/registry.py").read_text(encoding="utf-8")
    shared_keys_py = (repo_root / "shared_keys.py").read_text(encoding="utf-8")

    slug_pattern = _grep(
        registry_py, r'SLUG_PATTERN\s*=\s*re\.compile\(r"([^"]+)"\)', "registry.py"
    )
    app_env_pattern = _grep(
        shared_keys_py, r'_APP_ENV_RE\s*=\s*re\.compile\(r"([^"]+)"\)', "shared_keys.py"
    )
    # PK composition: agent_pk() = APPENV#{app_env}#AGENT#{slug}. Assert the
    # source still composes it that way rather than trusting a template blindly.
    prefix = _grep(shared_keys_py, r'APP_ENV_PREFIX\s*=\s*"([^"]+)"', "shared_keys.py")
    agent_pk_body = _grep(
        shared_keys_py, r'(?s)def agent_pk\([^)]*\).*?return (f"[^"]+")', "shared_keys.py"
    )
    if agent_pk_body != 'f"{_app(app_env)}#AGENT#{slug}"':
        raise SystemExit(f"sync_contract: agent_pk composition changed: {agent_pk_body}")

    max_soul = int(
        _grep(index_py, r"^_MAX_SOUL_BYTES\s*=\s*(\d+)\s*\*", "index.py")
    ) * 1024

    contract = {
        "contract_version": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "source": {
            "repo": "oppizi/agent-infra",
            "files": [
                "lambda/ui_admin/index.py",
                "scripts/agent_manager/registry.py",
                "shared_keys.py",
            ],
        },
        "pk_template": prefix + "{app_env}#AGENT#{slug}",
        "slug_pattern": slug_pattern,
        "app_env_pattern": app_env_pattern,
        # The frozen fixture is stricter than shared_keys' regex; the fixture wins.
        "app_envs": ["dev", "staging", "prod"],
        "dynamo_allowed": _set_literal(index_py, "_DYNAMO_ALLOWED", "index.py"),
        "config_allowed": _set_literal(index_py, "_CONFIG_ALLOWED", "index.py"),
        "frameworks": _set_literal(index_py, "_FRAMEWORK_VALUES", "index.py"),
        "framework_glosses": FRAMEWORK_GLOSSES,
        "visibility_values": _set_literal(index_py, "_VISIBILITY_VALUES", "index.py"),
        "max_soul_bytes": max_soul,
        # Server predicate (index.py create_agent_route): non-empty after
        # .strip(), length cap applies to the value sent. Converter emits the
        # stripped value, so the cap is enforced on the emitted string.
        "display_name": {"max_len": 256, "must_be_nonempty_stripped": True},
        "model_aliases": MODEL_ALIASES,
        # Create-time CONFIG-row constants for a channelless agent
        # (mirrors preflight/expected_channelless_config.json required_exact).
        "projection_constants": {
            "SK": "CONFIG",
            "agentType": "slack",
            "appId": "",
            "secretName": "",
            "runtimeArn": "",
            "endpointId": "",
            "slackAuthorized": False,
            "registrationOpen": "false",
            "visibility": "private",
        },
    }

    sanity = {"maverick", "nanobot", "openclaw"}
    if set(contract["frameworks"]) != sanity:
        raise SystemExit(f"sync_contract: framework set drifted: {contract['frameworks']}")
    if set(contract["framework_glosses"]) != set(contract["frameworks"]):
        raise SystemExit("sync_contract: glosses out of sync with frameworks")
    return contract


def main() -> None:
    here = Path(__file__).resolve()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", type=Path, default=here.parents[3])
    ap.add_argument("--out", type=Path, default=here.parents[1] / "plugin/contract.json")
    args = ap.parse_args()

    contract = extract(args.repo_root)
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
