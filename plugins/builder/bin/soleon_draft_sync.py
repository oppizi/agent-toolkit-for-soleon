#!/usr/bin/env python3
"""PostToolUse hook: a save under `.soleon/agents/<slug>/` becomes a
`patch_agent_draft` on the Soleon platform, instantly (spec D5, D17).

Wired by the plugin's `hooks/hooks.json` on `Write|Edit|MultiEdit`. Reads the
hook payload on stdin, and:

* exits 0 immediately for any path that is not a synced artefact of a pulled
  agent (`SOUL.md`, `config.json`, `skills/<id>/...`, `evals/<id>.json`) — the
  hook must be free for every other file in the project;
* otherwise maps the file to a partial FLAT document (`soul`, the config-derived
  flat fields, the rebuilt `skills` list, the rebuilt `evals`) through
  `soleon_agent_document.py`, and calls `patch_agent_draft` with
  `expected_updated_at = pull.json.draftEtag` (omitted when the pull found no
  draft — the first write then CREATES the draft);
* on success writes the new `draftEtag` into `pull.json`, then re-pulls and
  re-materializes the local copy (`soleon_pull_refresh.refresh`, which also
  re-syncs the test-chat sandbox via `sync_draft_test_chat`) so that BOTH the
  platform draft and the subagent definition the next run spawns from carry the
  save, and prints `{"systemMessage": ...}` on stdout (exit 0). A save that
  moved only the draft leaves the agent running the rules it was materialized
  with — see `_rebuild_local_copy`;
* on a CONFLICT (someone changed the draft elsewhere — the Soleon editor, another
  session) writes the current platform draft to `.pull/conflict.json`, prints the
  two ways out on STDERR and exits 2, which stops the session and makes Claude
  ask the person: reload theirs (`/pull-agent <slug>` again) or overwrite with
  the local version (`pull_agent.py adopt-etag` then save again);
* any other error → stderr + exit 2, never a silent skip.

Token handling is `soleon_mcp_client.py`'s (Claude Code's own OAuth token; never
logged).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soleon_agent_document as doc  # noqa: E402
import soleon_pull_refresh as pull_refresh  # noqa: E402  (refresh() only; no cycle — it imports neither this module nor the hook)
from soleon_mcp_client import (  # noqa: E402
    DEFAULT_SERVER_URL,
    SoleonClientError,
    SoleonMcpClient,
    ToolError,
    describe_error,
    is_platform_error,
)

EXIT_OK = 0
EXIT_BLOCK = 2


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


def file_path_of(hook: Dict[str, Any]) -> Optional[str]:
    tool_input = hook.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    fp = tool_input.get("file_path") or tool_input.get("path")
    return str(fp) if fp else None


# ---------------------------------------------------------------------------
# file → changes
# ---------------------------------------------------------------------------

def build_changes(agent_dir: Path, kind: str, detail: str) -> Dict[str, Any]:
    """The partial flat document for `patch_agent_draft`."""
    snapshot = doc.load_snapshot_document(agent_dir)
    if kind == "soul":
        return {"soul": (agent_dir / "SOUL.md").read_text(encoding="utf-8")}
    if kind == "config":
        config = doc.load_json(agent_dir / "config.json")
        if not isinstance(config, dict):
            raise ValueError("config.json must hold a JSON object")
        return doc.config_to_flat_changes(config)
    if kind == "skill":
        return {"skills": doc.read_skills(agent_dir, snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else None)}
    if kind == "eval":
        evals = snapshot.get("evals") if isinstance(snapshot.get("evals"), dict) else {}
        merged = dict(evals)
        merged["standardEvals"] = doc.read_evals(agent_dir, doc.standard_evals_of(snapshot))
        for key in ("sessionCriteria", "requestCriteria"):
            merged.setdefault(key, [])
        return {"evals": merged}
    raise ValueError("unknown artefact kind {!r}".format(kind))


# ---------------------------------------------------------------------------
# platform call
# ---------------------------------------------------------------------------

def _conflict_message(slug: str, agent_dir: Path) -> str:
    return (
        "The Soleon draft for {slug!r} was changed elsewhere (Soleon editor or another session), "
        "so this save was NOT applied. The platform's current draft is saved at "
        "{conflict}. Ask the user which way to go:\n"
        "  1. reload theirs — re-run `/pull-agent {slug}` (local edits are replaced by the platform draft), or\n"
        "  2. overwrite with the local version — run `python3 <plugin>/skills/pull-agent/assets/pull_agent.py "
        "adopt-etag --dir {dir}` and save the file again.\n"
        "Stop and ask; do not pick for them."
    ).format(slug=slug, conflict=agent_dir / doc.PULL_DIR / doc.CONFLICT_JSON, dir=agent_dir)


def _rebuild_local_copy(agent_dir: Path, client, slug: str, app_env: str,
                        runner=subprocess.run) -> str:
    """Re-pull and re-materialize after a save landed, so the subagent
    definition the NEXT run spawns from carries what was just saved.

    Without this the save is only half-applied: the platform draft moves, but
    `~/.claude/agents/<slug>.md` keeps the prompt it was last materialized
    with, and every run — including one started later in this same turn —
    reasons under the old rules. The person then gets a refusal citing the old
    configuration and no hint that the file is stale (2026-09-21,
    fund-raising-agent: a SOUL.md exception was saved, and the agent declined
    to act on it three times).

    The read-back is not optional: the subagent body is the PLATFORM-RENDERED
    system prompt (`pull_agent.py` materialize → `patch_prompt_paths`), not the
    local SOUL.md, so re-materializing from local files alone would rebuild the
    file from the old prompt and change nothing. `refresh()` re-syncs the test
    chat as part of the same read sequence, so the save keeps that guarantee.

    Never raises: the save itself already succeeded on the platform, so a
    failed rebuild is a loud note, not a blocked session.
    """
    try:
        envelope = client.call_tool("get_agent_draft", {"slug": slug, "app_env": app_env})
        if is_platform_error(envelope):
            raise pull_refresh.RefreshError("get_agent_draft: {}".format(describe_error(envelope)))
        draft_raw = doc.unwrap(envelope)
        if not isinstance(draft_raw, dict) or not isinstance(draft_raw.get("agent"), dict):
            raise pull_refresh.RefreshError("unexpected get_agent_draft answer")
        before = doc.load_pull(agent_dir)
        pull_refresh.refresh(agent_dir, before, draft_raw, client, runner=runner)
        # materialize rewrites pull.json from the platform document, which drops
        # the save provenance written moments ago; refresh only restores its own
        # (refreshedAt/refreshedFrom). Put the save's back, and keep the
        # rebuild's draftEtag — that one came from the platform.
        fresh = doc.load_pull(agent_dir)
        for key in ("lastSyncedAt", "lastSyncedFile"):
            if before.get(key) not in (None, ""):
                fresh[key] = before[key]
        fresh["source"] = "draft"
        doc.save_pull(agent_dir, fresh)
    except (pull_refresh.RefreshError, SoleonClientError, ToolError, subprocess.TimeoutExpired,
            OSError, json.JSONDecodeError) as exc:
        return ("; the local copy was NOT rebuilt ({}) — a run started now would still use the OLD rules. "
                "Re-run /pull-agent {} before asking the agent to act on this change.".format(exc, slug))
    return ", test session re-synced, local copy and subagent definition rebuilt"


def sync(agent_dir: Path, kind: str, detail: str, client_factory=SoleonMcpClient,
         runner=subprocess.run) -> int:
    pull = doc.load_pull(agent_dir)
    slug = str(pull.get("slug") or agent_dir.name)
    server_url = str(pull.get("serverUrl") or DEFAULT_SERVER_URL)
    app_env = str(pull.get("appEnv") or "dev")
    changes = build_changes(agent_dir, kind, detail)
    if not changes:
        _stderr("nothing in {} maps onto the Soleon draft — no sync".format(detail))
        return EXIT_OK

    client = client_factory(server_url)
    arguments: Dict[str, Any] = {"slug": slug, "app_env": app_env, "changes": changes}
    expected = doc.etag_to_int(pull.get("draftEtag"))
    if expected is not None:
        arguments["expected_updated_at"] = expected

    try:
        envelope = client.call_tool("patch_agent_draft", arguments)
    except ToolError as exc:
        payload = exc.payload or {}
        if payload.get("conflict"):
            envelope = payload
        else:
            _stderr("Soleon refused the draft save: {}".format(describe_error(payload) if payload.get("error") else exc))
            return EXIT_BLOCK
    except SoleonClientError as exc:
        _stderr("Soleon draft save failed: {}".format(exc))
        return EXIT_BLOCK

    if isinstance(envelope, dict) and envelope.get("conflict"):
        current = envelope.get("current_draft") or {}
        doc.dump_json(agent_dir / doc.PULL_DIR / doc.CONFLICT_JSON, current)
        _stderr(_conflict_message(slug, agent_dir))
        return EXIT_BLOCK
    if is_platform_error(envelope):
        _stderr("Soleon draft save failed: {}".format(describe_error(envelope)))
        return EXIT_BLOCK

    body = doc.unwrap(envelope)
    if not isinstance(body, dict):
        _stderr("Unexpected patch_agent_draft answer: {}".format(json.dumps(envelope)[:500]))
        return EXIT_BLOCK
    new_etag = body.get("draft_etag") or body.get("draftEtag")
    if new_etag:
        pull["draftEtag"] = str(new_etag)
    pull["source"] = "draft"
    pull["lastSyncedAt"] = _now_iso()
    pull["lastSyncedFile"] = detail
    doc.save_pull(agent_dir, pull)

    synced_note = _rebuild_local_copy(agent_dir, client, slug, app_env, runner=runner)

    if body.get("no_change"):
        message = "Soleon draft already matched {} (etag {}){}".format(detail, pull.get("draftEtag"), synced_note)
    else:
        message = "Soleon draft updated from {} (etag {}{}){}".format(
            detail, pull.get("draftEtag"), ", draft created" if body.get("draft_created") else "", synced_note,
        )
    sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    sys.stdout.flush()
    return EXIT_OK


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(stream=None, client_factory=SoleonMcpClient, runner=subprocess.run) -> int:
    hook = read_hook_input(stream)
    fp = file_path_of(hook)
    if not fp:
        return EXIT_OK
    agent_dir = doc.find_agent_dir(fp)
    if agent_dir is None:
        return EXIT_OK
    hit = doc.classify(agent_dir, fp)
    if hit is None:
        return EXIT_OK
    if not (agent_dir / doc.PULL_JSON).is_file():
        return EXIT_OK  # not a pulled agent dir (no pull.json) — nothing to sync to
    kind, detail = hit
    try:
        return sync(agent_dir, kind, detail, client_factory=client_factory, runner=runner)
    except Exception as exc:  # noqa: BLE001 — surfaced, never silent
        _stderr("Soleon draft sync failed for {}: {}: {}".format(fp, type(exc).__name__, exc))
        return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
