#!/usr/bin/env python3
"""UserPromptSubmit hook: a pulled agent follows the platform.

Local ↔ platform sync used to be one-way — every local save became a
`patch_agent_draft`, but an edit made in the Soleon editor never came back
until the person re-ran `/pull-agent` (2026-09-20: "the local version needs
to stay in sync with the latest updates"). This hook closes the loop. Before
every prompt in a project that holds a pulled agent (`.soleon/agents/<slug>/
pull.json`) it asks the platform for the draft's version stamp; when the stamp
differs from the one the last pull (or save) recorded, it re-runs the pull
headlessly — the same reads the `/pull-agent` skill makes, then the same
`materialize` step — so the subagent that answers THIS prompt reasons with the
platform's current SOUL, config, skills, evals and system prompt.

Rules:
* Nothing on the wire when the version matches beyond the one
  `get_agent_draft` read — that is the per-prompt cost, and there is none at
  all in a project with no pulled agent.
* The local `workspace/` is never touched: it is session data (memory, notes)
  the person may have edited, not agent definition.
* A pending save CONFLICT (`.pull/conflict.json`, written by the save hook)
  wins: the platform is NOT pulled over local edits that failed to push; the
  person is reminded to resolve it (reload theirs, or adopt-etag and save).
* Never blocks the prompt (exit 0 always). A refresh that fails is reported as
  context Claude relays, with the local copy left as it was.
* Output: JSON with `systemMessage` (shown to the person) and
  `hookSpecificOutput.additionalContext` (so Claude knows the agent changed
  under it, e.g. mid-conversation) — silent when nothing changed.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soleon_agent_document as doc  # noqa: E402
from soleon_mcp_client import (  # noqa: E402
    DEFAULT_SERVER_URL,
    SoleonClientError,
    SoleonMcpClient,
    await_control,
    describe_error,
    is_platform_error,
)

EXIT_OK = 0
HOOK_EVENT = "UserPromptSubmit"
AGENTS_ROOT = Path(".soleon") / "agents"


def _stderr(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


def read_hook_input(stream=None) -> Dict[str, Any]:
    raw = (stream or sys.stdin).read()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def pulled_agent_dirs(cwd: Path) -> List[Path]:
    """Every `<cwd>/.soleon/agents/<slug>/` that holds a `pull.json`."""
    root = cwd / AGENTS_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / doc.PULL_JSON).is_file())


def version_of(draft_raw: Dict[str, Any]) -> str:
    """One string naming what the platform serves: the draft's etag when a
    draft exists, else the deployed baseline's."""
    if draft_raw.get("hasDraft", True) and draft_raw.get("draftEtag") not in (None, ""):
        return "draft:{}".format(draft_raw["draftEtag"])
    return "deployed:{}".format(draft_raw.get("baselineEtag") or "")


def local_version_of(pull: Dict[str, Any]) -> str:
    if pull.get("draftEtag") not in (None, ""):
        return "draft:{}".format(pull["draftEtag"])
    return "deployed:{}".format(pull.get("baselineEtag") or "")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RefreshError(Exception):
    pass


def _terminal(client, slug: str, app_env: str, name: str) -> Dict[str, Any]:
    """A local-control read followed to its terminal envelope; raises on
    `state: error` or a platform error so the caller keeps the old file."""
    envelope = client.call_tool(name, {"slug": slug, "app_env": app_env})
    if is_platform_error(envelope):
        raise RefreshError("{}: {}".format(name, describe_error(envelope)))
    envelope = await_control(client, slug, envelope, app_env=app_env)
    if isinstance(envelope, dict) and envelope.get("state") == "error":
        raise RefreshError("{}: {}".format(name, describe_error(envelope)))
    return envelope


def refresh(agent_dir: Path, pull: Dict[str, Any], draft_raw: Dict[str, Any], client,
            runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Dict[str, Any]:
    """Re-pull everything but the workspace and re-materialize. Returns the
    materialize summary. Raises RefreshError with the local copy untouched
    when a read fails BEFORE anything is written."""
    slug = str(pull.get("slug") or agent_dir.name)
    app_env = str(pull.get("appEnv") or "dev")
    plugin_root = Path(str(pull.get("pluginRoot") or os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1]))
    server_url = str(pull.get("serverUrl") or DEFAULT_SERVER_URL)
    model = str(pull.get("model") or "sonnet")
    # The subagent file goes where materialize puts it TODAY (the user scope,
    # ~/.claude/agents/), never where an older pull.json says it went. The
    # first version of this hook re-used pull.json's `subagentFile` — a
    # pre-user-scope pull had recorded the PROJECT `.claude/agents/` path — and
    # so recreated a project-scope copy that shadows the user-scope one; in a
    # folder the person has not trusted, Claude Code skips a project agent's
    # inline MCP servers, and the agent spawned with no tools and printed tool
    # names as text (2026-09-20, fund-raising-agent). materialize also removes a
    # stale project-scope copy, which undoes that damage on the next refresh.

    # Read everything first; only then write, so a failed read changes nothing.
    config_env = client.call_tool("get_agent_config", {"slug": slug, "app_env": app_env})
    if is_platform_error(config_env):
        raise RefreshError("get_agent_config: {}".format(describe_error(config_env)))
    skills_env = client.call_tool("get_agent_skills", {"slug": slug, "app_env": app_env, "content_mode": "full"})
    if is_platform_error(skills_env):
        raise RefreshError("get_agent_skills: {}".format(describe_error(skills_env)))
    sync_env = client.call_tool("sync_draft_test_chat", {"slug": slug, "app_env": app_env})
    if is_platform_error(sync_env):
        raise RefreshError("sync_draft_test_chat: {}".format(describe_error(sync_env)))
    tools_env = _terminal(client, slug, app_env, "list_agent_tools")
    prompt_env = _terminal(client, slug, app_env, "get_agent_system_prompt")

    pull_dir = agent_dir / doc.PULL_DIR
    pull_dir.mkdir(parents=True, exist_ok=True)
    doc.dump_json(pull_dir / doc.DRAFT_SNAPSHOT, draft_raw)
    doc.dump_json(pull_dir / "config.json", config_env)
    doc.dump_json(pull_dir / "skills.json", skills_env)
    doc.dump_json(agent_dir / "tools.json", tools_env)
    doc.dump_json(agent_dir / "prompt.json", prompt_env)

    cmd = [sys.executable, str(plugin_root / "skills" / "pull-agent" / "assets" / "pull_agent.py"), "materialize",
           "--slug", slug, "--dir", str(agent_dir), "--server-url", server_url, "--model", model,
           "--plugin-root", str(plugin_root), "--keep-workspace"]
    proc = runner(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RefreshError("materialize failed: {}".format((proc.stderr or proc.stdout or "").strip()[-800:]))
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError:
        summary = {}
    # materialize rewrites pull.json; keep the refresh provenance on it.
    fresh = doc.load_pull(agent_dir)
    fresh["refreshedAt"] = _now_iso()
    fresh["refreshedFrom"] = version_of(draft_raw)
    doc.save_pull(agent_dir, fresh)
    return summary if isinstance(summary, dict) else {}


def check_agent(agent_dir: Path, client_factory=SoleonMcpClient, runner=subprocess.run) -> Optional[str]:
    """One pulled agent: None when it already matches the platform, else a
    one-line message saying what happened (refreshed / conflict / failed)."""
    try:
        pull = doc.load_pull(agent_dir)
    except (OSError, json.JSONDecodeError) as exc:
        return "Soleon: {} has an unreadable pull.json ({}); not refreshed.".format(agent_dir.name, exc)
    slug = str(pull.get("slug") or agent_dir.name)
    app_env = str(pull.get("appEnv") or "dev")
    display = str(pull.get("displayName") or slug)

    try:
        client = client_factory(str(pull.get("serverUrl") or DEFAULT_SERVER_URL))
        draft_raw = doc.unwrap(client.call_tool("get_agent_draft", {"slug": slug, "app_env": app_env}))
    except SoleonClientError as exc:
        return "Soleon: could not check whether {} changed on the platform ({}); using the local copy.".format(display, exc)
    if not isinstance(draft_raw, dict) or not isinstance(draft_raw.get("agent"), dict):
        if isinstance(draft_raw, dict) and is_platform_error(draft_raw):
            return "Soleon: could not check whether {} changed on the platform ({}); using the local copy.".format(
                display, describe_error(draft_raw))
        return "Soleon: unexpected get_agent_draft answer for {}; using the local copy.".format(display)

    remote, local = version_of(draft_raw), local_version_of(pull)
    if remote == local:
        return None
    if (agent_dir / doc.PULL_DIR / doc.CONFLICT_JSON).is_file():
        return ("Soleon: {} changed on the platform ({} → {}) but a save conflict is pending in {} — NOT refreshed. "
                "Resolve it first: reload theirs with /pull-agent {}, or keep yours with `pull_agent.py adopt-etag` and save again."
                ).format(display, local, remote, agent_dir / doc.PULL_DIR / doc.CONFLICT_JSON, slug)
    try:
        summary = refresh(agent_dir, pull, draft_raw, client, runner=runner)
    except (RefreshError, SoleonClientError, subprocess.TimeoutExpired) as exc:
        return "Soleon: {} changed on the platform ({} → {}) but the refresh failed ({}); the local copy is unchanged.".format(
            display, local, remote, exc)
    return ("Soleon: {} was changed on the platform ({} → {}); the local copy was refreshed — SOUL.md, config.json, "
            "{} skill(s), {} eval(s), the system prompt and the subagent definition. workspace/ untouched."
            ).format(display, local, remote, len(summary.get("skills") or []), len(summary.get("evals") or []))


def main(argv: Optional[List[str]] = None, stream=None, client_factory=SoleonMcpClient, runner=subprocess.run) -> int:
    hook = read_hook_input(stream)
    cwd = Path(hook.get("cwd") or os.getcwd())
    messages = [m for m in (check_agent(d, client_factory=client_factory, runner=runner) for d in pulled_agent_dirs(cwd)) if m]
    if messages:
        text = "\n".join(messages)
        sys.stdout.write(json.dumps({
            "systemMessage": text,
            "hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": text},
        }) + "\n")
        sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
