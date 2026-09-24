"""The local layout of a pulled Soleon agent and its mapping onto the platform's
FLAT editor document — one module, used by BOTH directions so they cannot drift:

* `pull_agent.py materialize` (draft document → files on disk)
* `soleon_draft_sync.py`      (a saved file → `patch_agent_draft` changes)

Layout under `.soleon/agents/<slug>/`:

    SOUL.md                  the draft soul, byte-exact
    config.json              the NESTED config.json shape (see flat_document_to_config)
    skills/<id>/SKILL.md     the skill's content, byte-exact
    skills/<id>/skill.json   {id, name, description, enabled} — the entry's metadata
    skills/<id>/<file...>    text package files (binaries stay in the pull snapshot)
    evals/<evalId>.json      one standard eval per file
    evals/results/           local judge results (never synced)
    workflows/<id>/SKILL.md  generated workflow skills (never synced)
    workspace/               the read-only workspace snapshot (never synced)
    prompt.json, tools.json  the platform's assembled prompt + registered tools
    pull.json                {slug, source, draftEtag, pulledAt, model, serverUrl, ...}
    .pull/*.json             raw responses, incl. draft.json (the pulled document)

Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PULL_DIR = ".pull"
PULL_JSON = "pull.json"
DRAFT_SNAPSHOT = "draft.json"
CONFLICT_JSON = "conflict.json"
SKILL_META = "skill.json"

#: Flat loop fields ↔ `config.loop` keys. The Agent field is `loopDimensions`
#: but the config key is `dimensions` (the platform mirrors the rename).
_LOOP_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("effort", "effort"),
    ("loopDimensions", "dimensions"),
    ("interruption", "interruption"),
    ("progressVerbosity", "progressVerbosity"),
    ("turnSummary", "turnSummary"),
    ("tokenBudget", "tokenBudget"),
    ("dailyTokenBudget", "dailyTokenBudget"),
    ("dailyTotalTokenBudget", "dailyTotalTokenBudget"),
    # Each budget's on/off switch (the Loop tab's Unlimited toggle). Without
    # these a switched-OFF budget pulled as a bare number, and the platform
    # reads a number with no flag as ON (budget_contract.budget_setting): the
    # local config.json of daily-inbox-summary showed daily budgets of 500,000
    # that were in fact Unlimited, and a diagnosis blamed them (2026-09-23).
    # Same names on both sides (ui_admin_agents lifts flat → loop verbatim).
    ("tokenBudgetEnabled", "tokenBudgetEnabled"),
    ("dailyTokenBudgetEnabled", "dailyTokenBudgetEnabled"),
    ("dailyTotalTokenBudgetEnabled", "dailyTotalTokenBudgetEnabled"),
)
_DEFAULTS_FIELDS = ("model", "computeMode", "access", "accessUsers")
_PROMPT_CACHE_TTLS = ("5m", "1h")

#: Top-level document keys that are NOT behaviour and never come from config.json.
#: Kept here so the inverse mapping never invents them.
_PRESENTATION_KEYS = ("name", "description", "iconName", "iconColor", "framework",
                      "slackDefaultChannelId")


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def unwrap(envelope: Any) -> Any:
    """`{status, body}` (a REST-forwarded MCP result) → `body`; anything else as is."""
    if isinstance(envelope, dict) and "body" in envelope and "status" in envelope and len(envelope) <= 3:
        return envelope["body"]
    return envelope


def load_json(path: Path) -> Any:
    with open(str(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# flat document  →  nested config.json
# ---------------------------------------------------------------------------

def flat_document_to_config(flat: Dict[str, Any], deployed_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The nested `config.json` a deploy of this flat document would ship.

    Server mirror of `_flat_draft_config_patch` (agent-infra
    `lambda/ui_admin_agents/index.py`, "lift a flat draft Agent body's
    behavioral fields into a nested config patch") plus the two lifts its
    callers add in `_draft_deploy_patch_body`: the flat `evals` block and
    `automations` → `schedules`. Applied over the DEPLOYED config (when given)
    with the platform's `_deep_merge_patch` semantics, so keys the editor does
    not carry (`schedules` when the draft has none, `user_schedules_enabled`,
    unknown extensions) stay visible read-only.
    """
    patch: Dict[str, Any] = {}
    defaults: Dict[str, Any] = {}
    if flat.get("model"):
        defaults["model"] = flat["model"]
    if flat.get("computeMode"):
        defaults["computeMode"] = flat["computeMode"]
    if flat.get("access"):
        defaults["access"] = flat["access"]
    if flat.get("accessUsers") is not None:
        defaults["accessUsers"] = flat["accessUsers"]
    if defaults:
        patch["agents"] = {"defaults": defaults}
    if "guardrails" in flat:
        g = flat.get("guardrails")
        patch["guardrails"] = g if isinstance(g, dict) else None
    if flat.get("tools") is not None:
        patch["tools"] = flat["tools"]
    if isinstance(flat.get("promptCaching"), bool):
        patch["promptCaching"] = flat["promptCaching"]
    if flat.get("promptCacheTtl") in _PROMPT_CACHE_TTLS:
        patch["promptCacheTtl"] = flat["promptCacheTtl"]
    loop: Dict[str, Any] = {}
    for flat_key, cfg_key in _LOOP_FIELDS:
        v = flat.get(flat_key)
        if v is None:
            continue
        if flat_key in ("effort", "loopDimensions") and not isinstance(v, dict):
            continue
        if flat_key in ("interruption", "progressVerbosity") and not isinstance(v, str):
            continue
        if flat_key == "turnSummary" and not isinstance(v, bool):
            continue
        if flat_key.endswith("Budget") and (isinstance(v, bool) or not isinstance(v, (int, float))):
            continue
        if flat_key.endswith("BudgetEnabled") and not isinstance(v, bool):
            continue
        loop[cfg_key] = v
    if loop:
        patch["loop"] = loop
    if isinstance(flat.get("evals"), dict):
        patch["evals"] = flat["evals"]
    if isinstance(flat.get("subagents"), dict):
        patch["subagents"] = flat["subagents"]
    if isinstance(flat.get("automations"), list):
        patch["schedules"] = [
            _automation_to_schedule(a) for a in flat["automations"]
            if isinstance(a, dict) and (a.get("schedule") or a.get("prompt"))
        ]
    if "userSchedulesEnabled" in flat:
        patch["user_schedules_enabled"] = bool(flat.get("userSchedulesEnabled"))
    return deep_merge_patch(deployed_config or {}, patch)


def _automation_to_schedule(a: Dict[str, Any]) -> Dict[str, Any]:
    """Port of the SPA's `uiAutomationToScheduleRecord` (content fields)."""
    out = {
        "id": a.get("id") or "",
        "name": a.get("name") or "",
        "cron": a.get("schedule") or "",
        "prompt": a.get("prompt") or "",
        "timezone": a.get("timezone") or "America/New_York",
        "paused": bool(a.get("paused", False)),
    }
    if a.get("destinationRaw"):
        out["destination"] = a["destinationRaw"]
    return out


def deep_merge_patch(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """The platform's `_deep_merge_patch`: dicts recurse, `None` deletes, lists
    and primitives replace. Pure."""
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
            continue
        current = out.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            out[key] = deep_merge_patch(current, value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# nested config.json  →  flat document changes (the INVERSE, for the save hook)
# ---------------------------------------------------------------------------

def config_to_flat_changes(config: Dict[str, Any]) -> Dict[str, Any]:
    """Invert `_flat_draft_config_patch` (agent-infra
    `lambda/ui_admin_agents/index.py`): which top-level FLAT document keys the
    editor stores each config.json value under. Also the server's own
    published→flat port (`_published_flat_agent` / `_flat_loop_fields`), which is
    the same table read in this direction.

        agents.defaults.model        → model
        agents.defaults.computeMode  → computeMode
        agents.defaults.access       → access
        agents.defaults.accessUsers  → accessUsers
        guardrails (dict | absent)   → guardrails (dict | False)
        tools (list)                 → tools
        promptCaching (bool)         → promptCaching
        promptCacheTtl ("5m"|"1h")   → promptCacheTtl
        loop.effort                  → effort
        loop.dimensions              → loopDimensions
        loop.interruption            → interruption
        loop.progressVerbosity       → progressVerbosity
        loop.turnSummary             → turnSummary
        loop.tokenBudget             → tokenBudget
        loop.dailyTokenBudget        → dailyTokenBudget
        loop.dailyTotalTokenBudget   → dailyTotalTokenBudget
        evals                        → evals
        subagents                    → subagents
        user_schedules_enabled       → userSchedulesEnabled

    `schedules` is deliberately NOT mapped: schedules are platform-only in local
    emulation (spec D13) and the editor's `automations` shape carries channel
    destinations the local copy cannot resolve — edit them in Soleon. Anything
    else in config.json (a legacy object-shaped `tools`, unknown extensions) is
    left alone rather than guessed.
    """
    changes: Dict[str, Any] = {}
    defaults = ((config.get("agents") or {}).get("defaults") or {}) if isinstance(config.get("agents"), dict) else {}
    for key in _DEFAULTS_FIELDS:
        if key in defaults:
            changes[key] = defaults[key]
    if "model" not in changes and config.get("model"):
        changes["model"] = config["model"]
    g = config.get("guardrails")
    changes["guardrails"] = g if isinstance(g, dict) else False
    if isinstance(config.get("tools"), list):
        changes["tools"] = config["tools"]
    if isinstance(config.get("promptCaching"), bool):
        changes["promptCaching"] = config["promptCaching"]
    if config.get("promptCacheTtl") in _PROMPT_CACHE_TTLS:
        changes["promptCacheTtl"] = config["promptCacheTtl"]
    loop = config.get("loop")
    if isinstance(loop, dict):
        for flat_key, cfg_key in _LOOP_FIELDS:
            if cfg_key in loop and loop[cfg_key] is not None:
                changes[flat_key] = loop[cfg_key]
    if isinstance(config.get("evals"), dict):
        changes["evals"] = config["evals"]
    if isinstance(config.get("subagents"), dict):
        changes["subagents"] = config["subagents"]
    if "user_schedules_enabled" in config:
        changes["userSchedulesEnabled"] = bool(config["user_schedules_enabled"])
    return changes


# ---------------------------------------------------------------------------
# Skills on disk
# ---------------------------------------------------------------------------

_TEXT_FILE_RE = re.compile(
    r"\.(md|txt|json|ya?ml|csv|toml|ini|cfg|py|js|ts|sh|html|css|xml|sql|tsv|rst|env)$", re.I,
)


def write_skills(agent_dir: Path, skills: List[Dict[str, Any]]) -> List[str]:
    """Materialize each skill entry as `skills/<id>/`. Returns the ids written."""
    written: List[str] = []
    root = agent_dir / "skills"
    for skill in skills or []:
        if not isinstance(skill, dict) or not skill.get("id"):
            continue
        sid = str(skill["id"])
        sdir = root / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "SKILL.md").write_text(str(skill.get("content") or ""), encoding="utf-8")
        meta = {
            "id": sid,
            "name": skill.get("name") or sid,
            "description": skill.get("description") or "",
            "enabled": bool(skill.get("enabled", True)),
        }
        dump_json(sdir / SKILL_META, meta)
        for entry in skill.get("files") or []:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            rel = str(entry["path"]).lstrip("/")
            if ".." in rel.split("/") or rel in ("SKILL.md", SKILL_META):
                continue
            if isinstance(entry.get("content"), str):
                target = sdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry["content"], encoding="utf-8")
        written.append(sid)
    return written


def read_skills(agent_dir: Path, snapshot_skills: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Rebuild the document's `skills` list from `skills/*/` — the list replaces
    wholesale on the platform, so every skill dir is one entry, in the pulled
    order first, new dirs appended alphabetically.

    Package files: text files on disk become `{path, content}` entries (an
    existing entry keeps its other keys, e.g. `enabled`); binary/oversize
    entries from the pull snapshot — which the platform never inlines — are
    round-tripped untouched, exactly as a files-unaware API client must.
    A text file deleted from disk drops its entry.
    """
    root = agent_dir / "skills"
    by_id = {str(s.get("id")): s for s in (snapshot_skills or []) if isinstance(s, dict) and s.get("id")}
    order = [sid for sid in by_id]
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if d.name not in order:
                order.append(d.name)
    out: List[Dict[str, Any]] = []
    for sid in order:
        sdir = root / sid
        if not (sdir / "SKILL.md").is_file():
            continue  # a deleted skill dir drops the entry
        prior = by_id.get(sid) or {}
        meta: Dict[str, Any] = {}
        if (sdir / SKILL_META).is_file():
            try:
                meta = load_json(sdir / SKILL_META)
            except (OSError, json.JSONDecodeError):
                meta = {}
        entry: Dict[str, Any] = {
            "id": sid,
            "name": meta.get("name") or prior.get("name") or sid,
            "description": meta.get("description") if "description" in meta else prior.get("description", ""),
            "content": (sdir / "SKILL.md").read_text(encoding="utf-8"),
            "enabled": bool(meta.get("enabled", prior.get("enabled", True))),
        }
        if isinstance(prior.get("emptyFolders"), list):
            entry["emptyFolders"] = prior["emptyFolders"]
        files: List[Dict[str, Any]] = []
        prior_files = {str(f.get("path")): f for f in (prior.get("files") or []) if isinstance(f, dict) and f.get("path")}
        on_disk: Dict[str, Path] = {}
        for p in sorted(sdir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(sdir).as_posix()
            if rel in ("SKILL.md", SKILL_META):
                continue
            on_disk[rel] = p
        for rel, p in on_disk.items():
            prev = dict(prior_files.get(rel) or {})
            if _TEXT_FILE_RE.search(rel) or "content" in prev:
                try:
                    prev["content"] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    if rel in prior_files:
                        files.append(prior_files[rel])
                    continue
            prev["path"] = rel
            prev.pop("stagedMissing", None)
            files.append(prev)
        for rel, prev in prior_files.items():
            if rel in on_disk:
                continue
            if "content" in prev:
                continue  # text file removed on disk → entry dropped
            files.append(prev)  # binary the local copy never held → round-trip
        if files:
            entry["files"] = files
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Evals on disk
# ---------------------------------------------------------------------------

def standard_evals_of(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`evals.standardEvals` from the flat document (or its legacy `config.evals`)."""
    evals = document.get("evals")
    if not isinstance(evals, dict):
        cfg = document.get("config")
        evals = cfg.get("evals") if isinstance(cfg, dict) else None
    if not isinstance(evals, dict):
        return []
    items = evals.get("standardEvals")
    return [e for e in items if isinstance(e, dict) and e.get("id")] if isinstance(items, list) else []


def _safe_eval_filename(eval_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", eval_id) or "eval"


def write_evals(agent_dir: Path, evals: List[Dict[str, Any]]) -> List[str]:
    root = agent_dir / "evals"
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for ev in evals:
        name = _safe_eval_filename(str(ev["id"])) + ".json"
        dump_json(root / name, ev)
        written.append(name)
    return written


def read_evals(agent_dir: Path, snapshot_evals: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Rebuild `evals.standardEvals` from `evals/*.json` (results/ excluded):
    pulled order first, new files appended alphabetically; a deleted file drops
    its eval. Each file must be one eval object with an `id`."""
    root = agent_dir / "evals"
    files: Dict[str, Dict[str, Any]] = {}
    if root.is_dir():
        for p in sorted(root.glob("*.json")):
            data = load_json(p)
            if not isinstance(data, dict) or not data.get("id"):
                raise ValueError("{} must hold one eval object with an 'id'".format(p))
            files[str(data["id"])] = data
    order = [str(e.get("id")) for e in (snapshot_evals or []) if isinstance(e, dict) and e.get("id")]
    for eid in files:
        if eid not in order:
            order.append(eid)
    return [files[eid] for eid in order if eid in files]


# ---------------------------------------------------------------------------
# Locating an agent dir from a saved file
# ---------------------------------------------------------------------------

def find_agent_dir(file_path: str) -> Optional[Path]:
    """`.../.soleon/agents/<slug>/...` → that `<slug>` dir, else None."""
    p = Path(file_path)
    parts = p.parts
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] == ".soleon" and i + 2 < len(parts) and parts[i + 1] == "agents":
            return Path(*parts[: i + 3])
    return None


def classify(agent_dir: Path, file_path: str) -> Optional[Tuple[str, str]]:
    """What a saved file under the agent dir maps to: ('soul'|'config'|'skill'|'eval', detail)
    or None when it is not a synced artefact (workspace, results, pull.json, .pull ...)."""
    try:
        rel = Path(file_path).resolve().relative_to(agent_dir.resolve()).as_posix()
    except ValueError:
        try:
            rel = Path(file_path).relative_to(agent_dir).as_posix()
        except ValueError:
            return None
    if rel == "SOUL.md":
        return ("soul", rel)
    if rel == "config.json":
        return ("config", rel)
    parts = rel.split("/")
    if parts[0] == "skills" and len(parts) >= 3:
        return ("skill", parts[1])
    if parts[0] == "evals" and len(parts) == 2 and parts[1].endswith(".json"):
        return ("eval", parts[1])
    return None


def load_pull(agent_dir: Path) -> Dict[str, Any]:
    return load_json(agent_dir / PULL_JSON)


def save_pull(agent_dir: Path, pull: Dict[str, Any]) -> None:
    dump_json(agent_dir / PULL_JSON, pull)


def load_snapshot_document(agent_dir: Path) -> Dict[str, Any]:
    """The pulled flat document (`.pull/draft.json`'s `agent`)."""
    try:
        raw = unwrap(load_json(agent_dir / PULL_DIR / DRAFT_SNAPSHOT))
    except (OSError, json.JSONDecodeError):
        return {}
    agent = raw.get("agent") if isinstance(raw, dict) else None
    return agent if isinstance(agent, dict) else {}


def etag_to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
