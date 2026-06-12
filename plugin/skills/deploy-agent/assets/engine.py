"""CliEngine — the single seam between the converter and the `allium` engine.

Resolution order: bundled binary (plugin/bin/allium-{os}-{arch}) first, PATH
fallback second. The PATH fallback is version-pinned to contract.json's
engine_version because every parsed-output fact (diagnostics shape, raw
default_expr semantics, exit-code behavior) was verified against that exact
version; set ALLIUM_ENGINE_UNPINNED=1 to accept a mismatched PATH binary.

Future control swaps (pure-Python reader, wasm) are new implementations of
this module's three functions — not surgery on the converter.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SUBPROCESS_TIMEOUT_S = 30

# platform.machine() varies by OS for the same silicon (F4).
_ARCH_NORMALIZE = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}


class EngineError(Exception):
    """Base — every engine failure is loud and carries a next action."""


class EngineNotFound(EngineError):
    pass


class EngineVersionMismatch(EngineError):
    pass


class EngineProtocol(EngineError):
    """The binary ran but its output violated the expected JSON contract."""


class ContractError(EngineError):
    pass


def plugin_root() -> Path:
    """Locate the plugin root that holds bin/ and contract.json.

    CLAUDE_PLUGIN_ROOT wins when the host sets it; otherwise fall back to
    this file's location: assets/ -> deploy-agent/ -> skills/ -> plugin/.
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[3])
    for c in candidates:
        if (c / "contract.json").is_file():
            return c
    attempted = " , ".join(str(c) for c in candidates)
    raise ContractError(
        "Cannot locate plugin root (no contract.json found). Attempted: "
        f"{attempted}. Fix: reinstall the plugin, or set CLAUDE_PLUGIN_ROOT "
        "to the directory containing contract.json."
    )


def load_contract(root: Path | None = None) -> dict:
    root = root or plugin_root()
    path = root / "contract.json"
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ContractError(
            f"contract.json is missing or corrupt at {path}: {e}. "
            "Fix: reinstall the plugin from its source. "
            "Maintainers: regenerate with harness/sync_contract.py."
        ) from e
    required = {
        "contract_version", "engine_version", "pk_template", "slug_pattern",
        "app_envs", "dynamo_allowed", "config_allowed", "frameworks",
        "visibility_values", "max_soul_bytes", "display_name",
        "model_aliases", "projection_constants",
    }
    missing = required - contract.keys()
    if missing:
        raise ContractError(
            f"contract.json self-check failed — missing keys: {sorted(missing)}. "
            "Fix: reinstall the plugin from its source. "
            "Maintainers: regenerate with harness/sync_contract.py."
        )
    if contract["contract_version"] != 1:
        raise ContractError(
            f"contract.json version {contract['contract_version']} is not supported "
            "by this converter (expects 1). Fix: reinstall the plugin so the "
            "converter and contract ship from the same bundle."
        )
    return contract


def _expected_binary_name() -> str:
    os_name = {"darwin": "darwin", "linux": "linux"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = _ARCH_NORMALIZE.get(machine, machine)
    return f"allium-{os_name}-{arch}"


def _binary_version(binary: str) -> str:
    out = _run([binary, "--version"], parse_json=False)
    # "allium 3.2.4 (language versions: 1, 2, 3)" — token index 1, never the last.
    tokens = out.split()
    if len(tokens) < 2 or tokens[0] != "allium":
        raise EngineProtocol(f"Unexpected --version output from {binary!r}: {out!r}")
    return tokens[1]


_PINNED_INSTALL = (
    "cargo install --git https://github.com/juxt/allium-tools --tag v{version} allium-cli"
)


def resolve(contract: dict, root: Path | None = None) -> tuple[str, str]:
    """Return (binary_path, resolved_version). Bundled-first, PATH-fallback."""
    root = root or plugin_root()
    pinned = contract["engine_version"]
    name = _expected_binary_name()
    bundled = root / "bin" / name
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled), _binary_version(str(bundled))

    on_path = shutil.which("allium")
    if on_path:
        version = _binary_version(on_path)
        if version != pinned and os.environ.get("ALLIUM_ENGINE_UNPINNED") != "1":
            raise EngineVersionMismatch(
                f"PATH allium is {version}, but this plugin is verified against "
                f"{pinned} (no bundled binary for your platform: expected "
                f"bin/{name}). Fix: install the pinned version with\n  "
                + _PINNED_INSTALL.format(version=pinned)
                + "\nor set ALLIUM_ENGINE_UNPINNED=1 to accept the mismatch at "
                "your own risk."
            )
        return on_path, version

    raise EngineNotFound(
        f"No allium engine found. Expected bundled binary bin/{name} under "
        f"{root}, and no `allium` on PATH. Fix: install the pinned version with\n  "
        + _PINNED_INSTALL.format(version=pinned)
        + f"\n(supported bundled platforms are listed in {root / 'README.md'})."
    )


def _run(argv: list[str], *, parse_json: bool, check_exit: bool = False):
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except subprocess.TimeoutExpired as e:
        raise EngineProtocol(
            f"Engine timed out after {SUBPROCESS_TIMEOUT_S}s: {' '.join(argv)}"
        ) from e
    except (UnicodeDecodeError, OSError) as e:
        raise EngineProtocol(f"Engine invocation failed for {' '.join(argv)}: {e}") from e

    if check_exit and proc.returncode != 0:
        raise EngineProtocol(
            f"Engine exited {proc.returncode} for {' '.join(argv)}.\n"
            f"stderr: {proc.stderr.strip()}"
        )
    if not parse_json:
        return proc.stdout.strip()
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise EngineProtocol(
            f"Engine emitted non-JSON output for {' '.join(argv)}: {e}.\n"
            f"stdout head: {proc.stdout[:400]!r}\nstderr: {proc.stderr.strip()[:400]}"
        ) from e


def check(binary: str, spec_path: Path) -> list[dict]:
    """Run `allium check`, return the parsed diagnostics list.

    IMPORTANT: allium check exits 1 on warnings-only output, so the exit code
    is meaningless — callers gate on parsed severity=="error" only.
    """
    out = _run([binary, "check", str(spec_path)], parse_json=True)
    diagnostics = out.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise EngineProtocol(f"check output missing diagnostics list: {out!r}")
    return diagnostics


def model(binary: str, spec_path: Path) -> dict:
    """Run `allium model`. Hard-fails iff the `config` array is absent.

    Verified live: `version: null` alone is NOT failure — a warnings-only spec
    (e.g. missing version marker) legitimately passes the check gate yet
    reports version null. Garbage input also returns exit 0, so config-array
    absence is the only reliable garbage signal (check-first ordering remains
    load-bearing for diagnostics quality).
    """
    out = _run([binary, "model", str(spec_path)], parse_json=True)
    if not isinstance(out.get("config"), list):
        raise EngineProtocol(
            f"allium model returned no config array for {spec_path} — the spec "
            "has no config block or the engine could not read it. The converter "
            "requires deploy parameters in a `config { }` block."
        )
    return out


def parse(binary: str, spec_path: Path) -> dict:
    out = _run([binary, "parse", str(spec_path)], parse_json=True)
    if "module" not in out:
        raise EngineProtocol(f"parse output missing module root for {spec_path}")
    return out


def selfcheck(root: Path | None = None) -> dict:
    """2-second preflight: contract loads, binary resolves, version matches.

    Returns a summary dict; raises EngineError with an actionable message on
    any failure. SKILL.md runs this as step 0 so a broken install fails before
    any LLM work is spent.
    """
    root = root or plugin_root()
    contract = load_contract(root)
    binary, version = resolve(contract, root)
    return {
        "plugin_root": str(root),
        "contract_version": contract["contract_version"],
        "engine_binary": binary,
        "engine_version": version,
        "engine_pinned": contract["engine_version"],
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        try:
            print(json.dumps(selfcheck(), indent=2))
        except EngineError as e:
            print(f"SELFCHECK FAILED: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(__doc__)
