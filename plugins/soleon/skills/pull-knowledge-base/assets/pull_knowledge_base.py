#!/usr/bin/env python3
"""Deterministic core of `/pull-knowledge-base` — turns the answers the skill
saved from the Soleon MCP into local files, and checks a push before the skill
makes it.

The skill (the LLM head) makes every MCP call and saves each RAW answer with
the Write tool; this script never sees a token and never touches the network.

Subcommands (all offline):

    plan          --dir D   which source ids still need a get_kb_source answer
    materialize   --dir D   sources/, manifest.json, INDEX.md (the folder's one
                            document), and the project-wide ../INDEX.md
    status        --dir D   each local source file: unchanged | changed | missing
    push-check    --dir D --file sources/F [--overwrite-page-edits]
                            exit 0 = push may go; 2 = refused; 3 = page edits
                            on Soleon a re-ingest may overwrite (ask first)
    record-push   --dir D --file sources/F
                            exit 0 = recorded; 2 = Soleon refused the push;
                            1 = the text Soleon now holds is not the local file

D = .soleon/knowledge-bases/<slug, else kb id>/, holding the saved answers:

    .pull/kb.json             get_knowledge_base
    .pull/pages.json          list_kb_pages
    .pull/sources/<id>.json   get_kb_source(include_raw=true), one per source
    .push/<id>/source.json    get_kb_source(include_raw=true), read again before a push
    .push/<id>/pages.json     list_kb_pages, read again before a push
    .push/<id>/versions.json  kb_versions
    .push/<id>/result.json    manage_kb_source's answer
    .push/<id>/after.json     get_kb_source(include_raw=true), read back after the push

Platform facts mirrored here (agent-infra, origin/dev, 2026-11-06):
- only pasted, md and txt sources keep editable text
  (`lambda/ui_admin_knowledge/mcp_handlers.py` RAW_TEXT_EDITABLE_KINDS);
- get_kb_source returns at most 64KB of it, flagged `raw_truncated`, and
  manage_kb_source refuses `raw_text` over 64KB and any source whose stored
  text is over 64KB (`lambda/mcp_server/core/domain/knowledge_manager.py`
  RAW_TEXT_CAP_BYTES, `kb_admin.py` ManageKbSourceHandler);
- `sourceRevision` is absent on a source nobody has edited, and the platform
  reads that as 0 (`mcp_handlers.patch_source`: `int(row.get("sourceRevision") or 0)`);
- a page edit made on Soleon is a history row with op `admin_edit`; a source's
  own ingestion writes `ingest` / `update` rows; kb_versions returns at most 200.

Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EDITABLE_KINDS = ("pasted", "md", "txt")
REMOTE_KINDS = ("url", "mcp")
MCP_CHILD_KIND = "mcp_child"
RAW_CAP_BYTES = 64 * 1024
INGEST_OPS = ("ingest", "update")
VERSIONS_CAP = 200

FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
KB_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")

PULL_DIR = ".pull"
PUSH_DIR = ".push"
MANIFEST = "manifest.json"
SKILL = "pull-knowledge-base"

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {
    "LPT%d" % i for i in range(1, 10)}

EXIT_REFUSED = 2
EXIT_ASK = 3
EXIT_MISMATCH = 1


class Stop(Exception):
    """A refusal with a plain reason; printed as JSON by main()."""

    def __init__(self, reason: str, code: int = EXIT_REFUSED, **extra: Any):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# Reading the saved answers
# ---------------------------------------------------------------------------

def read_answer(path: Path, what: str) -> Tuple[int, Any]:
    """(status, body) of one saved MCP answer.

    Accepts the answer as the tool returned it — a `{status, body}` envelope
    (most kb tools) or a bare object (get_knowledge_base) — and Claude Code's own
    copy of a long answer, a JSON list of `{type: "text", text}` blocks."""
    if not path.is_file():
        raise Stop("{} is missing: save the {} answer at {}".format(path.name, what, path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Stop("{} is not JSON: save the {} answer exactly as returned".format(path, what))
    if isinstance(data, list) and data and all(isinstance(b, dict) and b.get("type") == "text" for b in data):
        try:
            data = json.loads("".join(str(b.get("text") or "") for b in data))
        except ValueError:
            raise Stop("{} holds text blocks that are not JSON: save the {} answer exactly as returned".format(
                path, what))
    if isinstance(data, dict) and "status" in data and "body" in data and len(data) <= 3:
        status = data["status"]
        return (status if isinstance(status, int) else 0), data["body"]
    return 200, data


def body_of(path: Path, what: str) -> Any:
    status, body = read_answer(path, what)
    if not 200 <= status < 300:
        raise Stop("{} failed on Soleon (HTTP {}): {}".format(what, status, json.dumps(body, ensure_ascii=False)[:400]))
    return body


def _source_id_of(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("sourceId", "source_id", "id"):
            if isinstance(item.get(key), str):
                return item[key]
        sk = str(item.get("SK") or "")
        if sk.startswith("SOURCE#"):
            return sk[len("SOURCE#"):]
    return None


def _check_source_id(sid: str) -> str:
    if not SOURCE_ID_RE.match(sid):
        raise Stop("source id {!r} is not a Soleon source id; refusing to use it as a file name".format(sid))
    return sid


def revision_of(row: Dict[str, Any]) -> int:
    """The source's revision as Soleon checks it: a row nobody edited carries
    none, which the platform reads as 0."""
    raw = row.get("sourceRevision")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def page_path(page: Dict[str, Any]) -> str:
    """A page's path is its SK after `PAGE#`; the title is not always the path."""
    sk = str(page.get("SK") or "")
    if sk.startswith("PAGE#"):
        return sk[len("PAGE#"):]
    return str(page.get("path") or page.get("title") or "")


def folder_name(kb: Dict[str, Any]) -> str:
    slug = kb.get("slug")
    if isinstance(slug, str) and FOLDER_RE.match(slug):
        return slug
    kb_id = kb.get("kbId")
    if isinstance(kb_id, str) and KB_ID_RE.match(kb_id):
        return kb_id
    raise Stop("get_knowledge_base answer carries no usable slug or kbId")


def _load_kb(d: Path) -> Dict[str, Any]:
    kb = body_of(d / PULL_DIR / "kb.json", "get_knowledge_base")
    if not isinstance(kb, dict) or not isinstance(kb.get("kbId"), str) or not KB_ID_RE.match(kb["kbId"]):
        raise Stop("{} is not a get_knowledge_base answer (no kbId)".format(d / PULL_DIR / "kb.json"))
    expected = folder_name(kb)
    if d.name != expected:
        raise Stop("this base's folder is {!r} (its slug, else its id), not {!r}: save the answers under "
                   "{}".format(expected, d.name, d.parent / expected))
    return kb


def _pages_of(body: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """(pages, complete). list_kb_pages has no cursor argument, so a
    `nextCursor` means Soleon sent only part of the list."""
    if not isinstance(body, dict) or not isinstance(body.get("pages"), list):
        raise Stop("the list_kb_pages answer carries no pages[] list")
    pages = [p for p in body["pages"] if isinstance(p, dict)]
    return pages, not body.get("nextCursor")


def _fetched(d: Path) -> Dict[str, Path]:
    src_dir = d / PULL_DIR / "sources"
    if not src_dir.is_dir():
        return {}
    return {p.stem: p for p in sorted(src_dir.glob("*.json")) if SOURCE_ID_RE.match(p.stem)}


def planned_ids(kb: Dict[str, Any], pages: List[Dict[str, Any]], answers: Dict[str, Tuple[int, Any]]) -> List[str]:
    """Every source id this pull must read: the ones get_knowledge_base lists
    (newer servers), the ones each page names as its contributors, and the
    parent of any extracted MCP part already read."""
    ids: List[str] = []

    def add(sid: Optional[str]) -> None:
        if sid and sid not in ids:
            ids.append(_check_source_id(sid))

    for item in kb.get("sources") or []:
        add(_source_id_of(item))
    for page in pages:
        for sid in page.get("contributingSourceIds") or []:
            add(sid if isinstance(sid, str) else None)
    for sid in list(ids):
        status, body = answers.get(sid, (0, None))
        row = body.get("source") if isinstance(body, dict) else None
        if isinstance(row, dict) and row.get("sourceKind") == MCP_CHILD_KIND:
            add(row.get("parentSourceId") if isinstance(row.get("parentSourceId"), str) else None)
    return ids


def _plan(d: Path) -> Dict[str, Any]:
    kb = _load_kb(d)
    pages, complete = _pages_of(body_of(d / PULL_DIR / "pages.json", "list_kb_pages"))
    answers: Dict[str, Tuple[int, Any]] = {}
    for sid, path in _fetched(d).items():
        answers[sid] = read_answer(path, "get_kb_source")
    # An extracted MCP part names its parent only once it has been read, so a
    # parent shows up in toFetch on the run after its child's answer is saved.
    ids = planned_ids(kb, pages, answers)
    return {"kb": kb, "pages": pages, "pagesComplete": complete, "answers": answers, "ids": ids}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_stem(name: str) -> str:
    """A file name every OS accepts, from a source's file name or title."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"\.(md|markdown|txt)$", "", base, flags=re.I)
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii").lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", base)
    stem = re.sub(r"-{2,}", "-", stem).strip("-._")
    stem = stem[:80].rstrip("-._")
    if stem and stem.split(".")[0].upper() in WINDOWS_RESERVED:
        stem = "source-" + stem
    return stem


def _clip(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def about_line(text: Optional[str], kind: str) -> str:
    """One line on what a source is for: its first markdown heading, else its
    first sentence, cut to ~100 characters."""
    if text is None:
        return "(no text here: {})".format(kind)
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m and m.group(1).strip():
            return _clip(m.group(1))
    flat = " ".join(text.split())
    if not flat:
        return "(empty)"
    m = re.match(r"^(.+?[.!?])(?:\s|$)", flat)
    return _clip(m.group(1) if m else flat)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_to_ms(iso: str) -> int:
    dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms: Any) -> Optional[str]:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _write_text(path: Path, text: str) -> None:
    """Exact bytes: no newline translation, so a push sends what Soleon holds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise Stop("{} is not UTF-8 text".format(path))


def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# The generated documents
# ---------------------------------------------------------------------------

#: The one line an INDEX.md carries under its title: only a program writes it.
INDEX_HEADER = "<!-- Written by /{} from Soleon. Don't edit it: it's rewritten on every pull. -->".format(SKILL)


def base_index_text(manifest: Dict[str, Any]) -> str:
    """<base>/INDEX.md, the folder's one document: what the folder is, how to
    use it, then one line per source. Rewritten on every pull and push."""
    name, kb_id = manifest["name"], manifest["kbId"]
    lines = [
        "# {}".format(name),
        "",
        INDEX_HEADER,
        "",
        "This folder holds the **sources** of a Soleon knowledge base that an agent searches: "
        "{} (`{}`). It is not the agent manager's own notes or memory. It is the content the "
        "knowledge base is built from.".format(name, kb_id),
        "",
        "**A snapshot.** Pulled {} (UTC). It never updates by itself. Pull again before you rely on it or "
        "change it.".format(manifest["pulledAt"]),
        "",
        "**Edit and push only static sources:** text the team writes and keeps (pasted text, or a .md or "
        ".txt document). Pushing replaces the source on Soleon and re-ingests it: Soleon rebuilds its pages "
        "and facts, which costs money, and the change reaches every environment at once (knowledge bases "
        "have no draft). Push only when the builder says so.",
        "",
        "**Other ways a knowledge base is managed, not from here:**",
        "",
        "- A web-page source refreshes itself on its schedule.",
        "- A source from another system (an MCP source) is extracted by Soleon.",
        "- A page can be edited directly in Soleon (`put_kb_page`). A re-ingest of its source may "
        "overwrite that edit.",
        "- What the agent actually reads is the pages, so check them live (`list_kb_pages`, `get_kb_page`).",
        "",
        "**What is here:** `sources/` holds one file per source that has stored text, exactly as Soleon "
        "holds it. `manifest.json` records this pull: for each source its id, kind, title, revision "
        "(needed to push), local file (or none), and for web-page and MCP sources their address or "
        "connection and schedule. `.pull/` and `.push/` keep the raw answers read from Soleon.",
        "",
        "## Sources",
        "",
        "| Source | Kind | Revision | What it's for |",
        "|---|---|---|---|",
    ]
    for s in manifest["sources"]:
        if s.get("localFile"):
            label = "[{}]({})".format(_cell(Path(s["localFile"]).name), s["localFile"])
        else:
            label = _cell(s.get("title") or s.get("url") or s["id"])
            if s.get("tooLargeToEditHere"):
                label += " (too large to edit here)"
        lines.append("| {} | {} | {} | {} |".format(label, _cell(s.get("kind")), s.get("sourceRevision", 0),
                                                    _cell(s.get("about") or "")))
    if not manifest["sources"]:
        lines.append("| (no sources found) | | | |")
    lines.append("")
    return "\n".join(lines)


def root_index_text(root: Path) -> str:
    """.soleon/knowledge-bases/INDEX.md: one line per base pulled into this project."""
    lines = [
        "# Knowledge bases pulled into this project",
        "",
        INDEX_HEADER,
        "",
        "Each folder here holds the sources of one Soleon knowledge base an agent searches, pulled on "
        "demand; its INDEX.md says what it holds and how to use it.",
        "",
    ]
    count = 0
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        mpath = folder / MANIFEST
        if not mpath.is_file():
            continue
        count += 1
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
            sources = m.get("sources") or []
            editable = sum(1 for s in sources if s.get("editable"))
            lines.append("- [{}]({}/INDEX.md) (`{}`), folder `{}/`: pulled {}, {} source{} ({} editable text)".format(
                m.get("name") or folder.name, folder.name, m.get("kbId"), folder.name, m.get("pulledAt"),
                len(sources), "" if len(sources) == 1 else "s", editable))
        except (ValueError, AttributeError, TypeError):
            lines.append("- `{}/`: manifest.json unreadable; pull this base again".format(folder.name))
    if not count:
        lines.append("(none)")
    lines.append("")
    return "\n".join(lines)


def _write_docs(d: Path, manifest: Dict[str, Any]) -> Dict[str, str]:
    index = d / "INDEX.md"
    root_index = d.parent / "INDEX.md"
    _write_text(index, base_index_text(manifest))
    # One document per folder: an earlier pull's README.md is retired.
    old_readme = d / "README.md"
    if old_readme.is_file():
        old_readme.unlink()
    _write_text(root_index, root_index_text(d.parent))
    return {"index": str(index), "rootIndex": str(root_index)}


# ---------------------------------------------------------------------------
# plan / materialize / status
# ---------------------------------------------------------------------------

def cmd_plan(d: Path) -> Dict[str, Any]:
    p = _plan(d)
    to_fetch = [i for i in p["ids"] if i not in p["answers"]]
    return {"kbId": p["kb"]["kbId"], "toFetch": to_fetch, "found": len(p["ids"]),
            "sourceCount": p["kb"].get("sourceCount"), "pagesComplete": p["pagesComplete"]}


def _source_entry(sid: str, body: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str], bool]:
    """(manifest entry, text, text is whole) for one get_kb_source answer."""
    row = body.get("source") if isinstance(body.get("source"), dict) else {}
    kind = str(row.get("sourceKind") or "unknown")
    raw = body.get("raw_text") if isinstance(body.get("raw_text"), str) else None
    truncated = bool(body.get("raw_truncated"))
    entry: Dict[str, Any] = {
        "id": sid,
        "kind": kind,
        "title": row.get("title") or row.get("filename") or sid,
        "filename": row.get("filename"),
        "sourceRevision": revision_of(row),
        "localFile": None,
        "editable": False,
        "sha256": None,
        "bytes": None,
        "tooLargeToEditHere": False,
        "ingestStatus": row.get("ingestStatus") or row.get("extractionStatus"),
        "lastIngestedAt": row.get("lastIngestedAt"),
        "pagesContributed": row.get("pagesContributed"),
        "note": None,
    }
    if kind in EDITABLE_KINDS:
        if raw is None:
            entry["note"] = "Soleon returned no stored text for this source; pull again with include_raw=true"
        elif truncated:
            entry["tooLargeToEditHere"] = True
            entry["note"] = ("too large to edit here: Soleon returns only the first 64KB of its text, and a push "
                             "replaces the whole text; correct it in the Soleon console")
        entry["about"] = about_line(raw, kind)
        return entry, raw, raw is not None and not truncated
    if kind == "url":
        entry["url"] = row.get("sourceUrl")
        entry["schedule"] = row.get("schedule") or None
        entry["note"] = "a web page: Soleon fetches it again on its schedule; its text stays on Soleon"
    elif kind == "mcp":
        entry["connection"] = row.get("mcpConnectionId")
        entry["toolAllowlist"] = row.get("mcpToolAllowlist") or []
        entry["schedule"] = row.get("schedule") or None
        entry["childSourceIds"] = []
        entry["note"] = "from another system: Soleon extracts it through the connection; its text stays on Soleon"
    elif kind == MCP_CHILD_KIND:
        entry["parentSourceId"] = row.get("parentSourceId")
        entry["note"] = "a part Soleon extracted for an MCP source whose own row could not be read"
    else:
        entry["note"] = "stored on Soleon as a {} file; no editable text".format(kind)
    entry["about"] = about_line(None, kind)
    return entry, None, False


def materialize(d: Path, now: Optional[str] = None) -> Dict[str, Any]:
    d = d.resolve()
    p = _plan(d)
    kb = p["kb"]
    missing = [i for i in p["ids"] if i not in p["answers"]]
    if missing:
        raise Stop("no get_kb_source answer saved yet for: {}. Read each with get_kb_source(include_raw=true) into "
                   "{}/<id>.json, then run materialize again".format(", ".join(missing), d / PULL_DIR / "sources"),
                   toFetch=missing)
    warnings: List[str] = []
    if not p["pagesComplete"]:
        warnings.append("list_kb_pages returned part of the page list only, so sources may be missing")

    entries: List[Tuple[str, Dict[str, Any], Optional[str], bool]] = []
    children: Dict[str, List[str]] = {}
    for sid in p["ids"]:
        status, body = p["answers"][sid]
        if not 200 <= status < 300 or not isinstance(body, dict) or not isinstance(body.get("source"), dict):
            warnings.append("source {} could not be read (HTTP {}): {}".format(
                sid, status, json.dumps(body, ensure_ascii=False)[:200]))
            continue
        row = body["source"]
        parent = row.get("parentSourceId")
        if row.get("sourceKind") == MCP_CHILD_KIND and isinstance(parent, str) and parent in p["answers"] \
                and 200 <= p["answers"][parent][0] < 300:
            children.setdefault(parent, []).append(sid)
            continue
        entry, text, whole = _source_entry(sid, body)
        entries.append((str(row.get("createdAt") or ""), entry, text, whole))
    entries.sort(key=lambda e: (e[0], e[1]["id"]))

    stale = [i for i in _fetched(d) if i not in p["ids"]]
    if stale:
        warnings.append("ignored saved answers for sources no longer in this base: {}".format(", ".join(stale)))

    # Previous pull: remove the files it wrote, so a renamed or deleted source leaves nothing behind.
    old_manifest = d / MANIFEST
    if old_manifest.is_file():
        try:
            for s in json.loads(old_manifest.read_text(encoding="utf-8")).get("sources") or []:
                lf = s.get("localFile") if isinstance(s, dict) else None
                if isinstance(lf, str) and lf.startswith("sources/") and "/" not in lf[len("sources/"):]:
                    old = d / lf
                    if old.is_file():
                        old.unlink()
        except ValueError:
            warnings.append("the previous manifest.json was unreadable; old files in sources/ were left as they were")

    used: set = set()
    files: List[str] = []
    sources: List[Dict[str, Any]] = []
    for _created, entry, text, whole in entries:
        if entry["id"] in children:
            entry["childSourceIds"] = sorted(children[entry["id"]])
        if whole and text is not None:
            ext = ".md" if entry["kind"] == "md" else ".txt"
            # A file name is a path (keep its last part); a title is not (a "/" in it is just a character).
            stem = (safe_stem(entry["filename"]) if entry.get("filename")
                    else safe_stem(str(entry["title"]).replace("/", " ").replace("\\", " ")))
            stem = stem or safe_stem(entry["id"]) or "source"
            name = stem + ext
            if name in used:
                name = "{}-{}{}".format(stem, safe_stem(entry["id"]) or "source", ext)
            used.add(name)
            rel = "sources/" + name
            _write_text(d / rel, text)
            entry.update({"localFile": rel, "editable": True, "sha256": sha256_text(text),
                          "bytes": len(text.encode("utf-8"))})
            files.append(rel)
        sources.append(entry)

    untracked = sorted("sources/" + f.name for f in (d / "sources").glob("*")
                       if f.is_file() and "sources/" + f.name not in files) if (d / "sources").is_dir() else []
    if untracked:
        warnings.append("files in sources/ this pull did not write (left as they are): {}".format(", ".join(untracked)))
    count = kb.get("sourceCount")
    if isinstance(count, int) and count > len(sources):
        warnings.append("Soleon counts {} sources and {} were found: sources are found through the pages they built, "
                        "so a source that built no page (still ingesting, or failed) is not here".format(
                            count, len(sources)))

    manifest = {
        "schemaVersion": 1,
        "kbId": kb["kbId"],
        "slug": kb.get("slug"),
        "name": kb.get("displayName") or kb.get("slug") or kb["kbId"],
        "pulledAt": now or _now_iso(),
        "sourceCount": count,
        "pageCount": kb.get("pageCount"),
        "sources": sources,
        "warnings": warnings,
    }
    _dump_json(d / MANIFEST, manifest)
    docs = _write_docs(d, manifest)
    return {
        "kbId": manifest["kbId"], "slug": manifest["slug"], "name": manifest["name"], "folder": str(d),
        "pulledAt": manifest["pulledAt"], "sources": len(sources), "files": files,
        "editable": [s["localFile"] for s in sources if s["editable"]],
        "tooLargeToEditHere": [s["title"] for s in sources if s["tooLargeToEditHere"]],
        "remote": [{k: s.get(k) for k in ("id", "kind", "title", "url", "connection", "schedule") if s.get(k) is not None}
                   for s in sources if s["kind"] in REMOTE_KINDS],
        "otherKinds": [{"id": s["id"], "kind": s["kind"], "title": s["title"]} for s in sources
                       if s["kind"] not in EDITABLE_KINDS + REMOTE_KINDS],
        "warnings": warnings, **docs,
    }


def _load_manifest(d: Path) -> Dict[str, Any]:
    path = d / MANIFEST
    if not path.is_file():
        raise Stop("{} is missing: pull the knowledge base first".format(path))
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Stop("{} is not JSON: pull the knowledge base again".format(path))
    if not isinstance(m, dict) or not isinstance(m.get("sources"), list):
        raise Stop("{} is not a pull manifest: pull the knowledge base again".format(path))
    return m


def file_state(d: Path, entry: Dict[str, Any]) -> str:
    path = d / entry["localFile"]
    if not path.is_file():
        return "missing"
    return "unchanged" if sha256_text(_read_text(path)) == entry.get("sha256") else "changed"


def cmd_status(d: Path) -> Dict[str, Any]:
    m = _load_manifest(d)
    files = []
    for s in m["sources"]:
        if s.get("localFile"):
            files.append({"file": s["localFile"], "sourceId": s["id"], "state": file_state(d, s)})
    return {"kbId": m.get("kbId"), "pulledAt": m.get("pulledAt"),
            "changed": [f["file"] for f in files if f["state"] == "changed"],
            "missing": [f["file"] for f in files if f["state"] == "missing"],
            "unchanged": sum(1 for f in files if f["state"] == "unchanged"), "files": files}


# ---------------------------------------------------------------------------
# push-check / record-push
# ---------------------------------------------------------------------------

def _entry_for(d: Path, m: Dict[str, Any], file_arg: str) -> Dict[str, Any]:
    target = Path(file_arg)
    rel = target.resolve().relative_to(d) if target.is_absolute() and d in target.resolve().parents else target
    rel_s = str(rel).replace("\\", "/")
    if not rel_s.startswith("sources/"):
        rel_s = "sources/" + rel_s
    for s in m["sources"]:
        if s.get("localFile") == rel_s:
            return s
    stem = Path(rel_s).stem
    for s in m["sources"]:
        if not s.get("localFile") and stem in (safe_stem(s.get("filename") or ""), safe_stem(s.get("title") or "")):
            if s.get("tooLargeToEditHere"):
                raise Stop("{} is too large to edit here: Soleon returns only its first 64KB and a push replaces "
                           "the whole text; correct it in the Soleon console".format(s.get("title")))
            raise Stop("{} is a {} source: it has no editable text here".format(s.get("title"), s.get("kind")))
    raise Stop("{} is not a source file this pull wrote (see INDEX.md)".format(rel_s))


def _push_dir(d: Path, sid: str) -> Path:
    return d / PUSH_DIR / _check_source_id(sid)


def push_check(d: Path, file_arg: str, overwrite_page_edits: bool = False) -> Dict[str, Any]:
    d = d.resolve()
    m = _load_manifest(d)
    entry = _entry_for(d, m, file_arg)
    sid = entry["id"]
    if entry.get("tooLargeToEditHere"):
        raise Stop("{} is too large to edit here; correct it in the Soleon console".format(entry["title"]))
    if not entry.get("editable") or entry.get("kind") not in EDITABLE_KINDS:
        raise Stop("{} is a {} source: only pasted text, .md and .txt sources can be pushed".format(
            entry["title"], entry.get("kind")))
    local_path = d / entry["localFile"]
    if not local_path.is_file():
        raise Stop("{} is missing: nothing to push".format(entry["localFile"]))
    local = _read_text(local_path)
    if sha256_text(local) == entry.get("sha256"):
        raise Stop("{} is unchanged since it was pulled: nothing to push".format(entry["localFile"]))
    if not local.strip():
        raise Stop("{} is empty: Soleon refuses an empty source".format(entry["localFile"]))
    size = len(local.encode("utf-8"))
    if size > RAW_CAP_BYTES:
        raise Stop("{} is {} bytes; a push may carry at most {} (64KB), the most this toolkit can read back. "
                   "Correct a larger source in the Soleon console".format(entry["localFile"], size, RAW_CAP_BYTES))

    pd = _push_dir(d, sid)
    fresh = body_of(pd / "source.json", "get_kb_source")
    row = fresh.get("source") if isinstance(fresh, dict) else None
    if not isinstance(row, dict):
        raise Stop("{} is not a get_kb_source answer".format(pd / "source.json"))
    if _source_id_of(row) not in (sid, None):
        raise Stop("{} holds source {}, not {}".format(pd / "source.json", _source_id_of(row), sid))
    if row.get("sourceKind") not in EDITABLE_KINDS:
        raise Stop("source {} is now a {} source on Soleon: pull again".format(sid, row.get("sourceKind")))
    now_rev = revision_of(row)
    if now_rev != entry.get("sourceRevision", 0):
        raise Stop("someone saved this source on Soleon since the pull (revision {} then, {} now). Pull again, "
                   "then redo the edit".format(entry.get("sourceRevision", 0), now_rev),
                   pulledRevision=entry.get("sourceRevision", 0), currentRevision=now_rev)
    remote = fresh.get("raw_text")
    if not isinstance(remote, str):
        raise Stop("{} has no raw_text: read it with get_kb_source(include_raw=true)".format(pd / "source.json"))
    if fresh.get("raw_truncated"):
        raise Stop("the text on Soleon is now over 64KB, too large to edit here; correct it in the Soleon console")
    if sha256_text(remote) != entry.get("sha256"):
        raise Stop("the text of this source on Soleon changed since the pull. Pull again, then redo the edit")

    pages, _complete = _pages_of(body_of(pd / "pages.json", "list_kb_pages"))
    contributed = sorted(page_path(pg) for pg in pages if sid in (pg.get("contributingSourceIds") or []))
    versions_body = body_of(pd / "versions.json", "kb_versions")
    versions = versions_body.get("versions") if isinstance(versions_body, dict) else None
    if not isinstance(versions, list):
        raise Stop("{} carries no versions[] list".format(pd / "versions.json"))
    since = entry.get("pushedAt") or m["pulledAt"]
    since_ms = _iso_to_ms(since)
    wanted = set(contributed)
    edits = []
    for v in versions:
        if not isinstance(v, dict) or not isinstance(v.get("ts"), (int, float)) or v["ts"] <= since_ms:
            continue
        if v.get("op") in INGEST_OPS:
            continue
        touched = sorted({str(t.get("path")) for t in (v.get("pathsTouched") or [])
                          if isinstance(t, dict) and t.get("path") in wanted})
        if touched:
            edits.append({"when": _ms_to_iso(v["ts"]), "op": v.get("op"), "by": v.get("agent") or v.get("actor"),
                          "summary": v.get("summary"), "pages": touched})
    stamps = [v["ts"] for v in versions if isinstance(v, dict) and isinstance(v.get("ts"), (int, float))]
    reaches = not (len(versions) >= VERSIONS_CAP and stamps and min(stamps) > since_ms)

    remote_lines = remote.splitlines(keepends=True)
    local_lines = local.splitlines(keepends=True)
    diff = list(difflib.unified_diff(remote_lines, local_lines, "soleon/" + entry["localFile"], entry["localFile"]))
    added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    diff_file = pd / "diff.txt"
    _write_text(diff_file, "".join(diff))

    result = {
        "ok": True, "kbId": m["kbId"], "sourceId": sid, "title": entry["title"], "file": entry["localFile"],
        "expectedRevision": now_rev, "bytes": size, "linesAdded": added, "linesRemoved": removed,
        "diffFile": str(diff_file), "pagesRebuilt": contributed, "pageEditsSince": since,
        "pageEditsSincePull": edits, "historyReachesPull": reaches,
    }
    if (edits or not reaches) and not overwrite_page_edits:
        reason = ("pages this source built were edited on Soleon since {}; a re-ingest may overwrite those edits".format(since)
                  if edits else
                  "the change history Soleon returns (its newest {}) does not reach back to {}, so edits made on "
                  "Soleon since then cannot be ruled out".format(VERSIONS_CAP, since))
        raise Stop(reason, code=EXIT_ASK, **{k: v for k, v in result.items() if k != "ok"})
    return result


def record_push(d: Path, file_arg: str, now: Optional[str] = None) -> Dict[str, Any]:
    d = d.resolve()
    m = _load_manifest(d)
    entry = _entry_for(d, m, file_arg)
    sid = entry["id"]
    pd = _push_dir(d, sid)
    status, body = read_answer(pd / "result.json", "manage_kb_source")
    if status == 409 or (isinstance(body, dict) and body.get("code") == "SOURCE_CONFLICT"):
        raise Stop("Soleon refused the push: someone saved this source in between (revision now {}). Nothing was "
                   "changed. Pull again, then redo the edit".format(
                       body.get("currentRevision") if isinstance(body, dict) else "?"))
    if not 200 <= status < 300:
        raise Stop("Soleon refused the push (HTTP {}): {}".format(status, json.dumps(body, ensure_ascii=False)[:400]))
    patched = body.get("patched") if isinstance(body, dict) and isinstance(body.get("patched"), dict) else {}

    after = body_of(pd / "after.json", "get_kb_source")
    row = after.get("source") if isinstance(after, dict) and isinstance(after.get("source"), dict) else {}
    after_rev = revision_of(row)
    remote = after.get("raw_text") if isinstance(after, dict) else None
    if not isinstance(remote, str) or after.get("raw_truncated"):
        raise Stop("{} has no whole raw_text: read the source back with get_kb_source(include_raw=true)".format(
            pd / "after.json"))
    local = _read_text(d / entry["localFile"])
    stamp = now or _now_iso()
    entry["sourceRevision"] = after_rev
    entry["pushedAt"] = stamp
    entry["sha256"] = sha256_text(remote)
    entry["bytes"] = len(remote.encode("utf-8"))
    entry["about"] = about_line(remote, entry["kind"])
    entry["ingestStatus"] = row.get("ingestStatus") or entry.get("ingestStatus")
    matches = sha256_text(local) == entry["sha256"]
    _dump_json(d / MANIFEST, m)
    docs = _write_docs(d, m)
    out = {"ok": matches, "kbId": m["kbId"], "sourceId": sid, "file": entry["localFile"],
           "sourceRevision": after_rev, "patchedRevision": patched.get("sourceRevision"),
           "reingested": patched.get("reingested"), "ingestStatus": entry["ingestStatus"], **docs}
    if not matches:
        diff = list(difflib.unified_diff(remote.splitlines(keepends=True), local.splitlines(keepends=True),
                                         "soleon/" + entry["localFile"], entry["localFile"]))
        _write_text(pd / "after-diff.txt", "".join(diff))
        raise Stop("the text Soleon holds now is not the local file (a copying error in the push). The new "
                   "revision is recorded, so the file can be pushed again", code=EXIT_MISMATCH,
                   diffFile=str(pd / "after-diff.txt"), **{k: v for k, v in out.items() if k != "ok"})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="pull-knowledge-base deterministic core")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "materialize", "status"):
        sp = sub.add_parser(name)
        sp.add_argument("--dir", required=True)
    pc = sub.add_parser("push-check")
    pc.add_argument("--dir", required=True)
    pc.add_argument("--file", required=True)
    pc.add_argument("--overwrite-page-edits", action="store_true",
                    help="only after the builder said to overwrite the page edits listed by a previous run")
    rp = sub.add_parser("record-push")
    rp.add_argument("--dir", required=True)
    rp.add_argument("--file", required=True)
    args = ap.parse_args(argv)
    d = Path(args.dir)
    try:
        if args.cmd == "plan":
            out = cmd_plan(d.resolve())
        elif args.cmd == "materialize":
            out = materialize(d)
        elif args.cmd == "status":
            out = cmd_status(d.resolve())
        elif args.cmd == "push-check":
            out = push_check(d, args.file, args.overwrite_page_edits)
        else:
            out = record_push(d, args.file)
    except Stop as stop:
        print(json.dumps({"ok": False, "reason": stop.reason, **stop.extra}, indent=2, ensure_ascii=False))
        return stop.code
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
