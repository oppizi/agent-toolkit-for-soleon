"""pull_knowledge_base.py — the deterministic core of /pull-knowledge-base.

Against saved tool answers shaped like the live ones (get_knowledge_base with
no source list, list_kb_pages in a {status, body} envelope, get_kb_source with
include_raw): which sources a pull must read, the files it writes, the
manifest, the two INDEX.md files (no README.md), safe file names, and every
push check (revision moved, text changed, page edits since the pull, history
too short, non-text, too large, unchanged) with the record step after a push.
No network: the "fakes" are the answers the skill would have saved."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
ASSETS = BET_ROOT / "plugins" / "soleon" / "skills" / "pull-knowledge-base" / "assets"
SCRIPT = ASSETS / "pull_knowledge_base.py"
sys.path.insert(0, str(ASSETS))
import pull_knowledge_base as pkb  # noqa: E402

KB_ID = "ab12cd34ef56"
SLUG = "demo-kb"
NAME = "Demo Knowledge | Base"
PULLED_AT = "2026-11-06T12:00:00Z"
PULLED_MS = 1793966400000  # PULLED_AT in ms
HEADER = "<!-- Written by /pull-knowledge-base from Soleon. Don't edit it: it's rewritten on every pull. -->"

PASTED_TEXT = "Opening hours are 9 to 5. Closed on public holidays.\r\nAsk the team for anything else.\n"
MD_TEXT = "```\n# not a heading\n```\n\n# Pricing rules\n\nPrices are quoted per campaign.\n"
BIG_TEXT = "# Big catalogue\n\n" + "row\n" * 100


def _source_row(sid, kind, created, **extra):
    row = {"PK": "KB#" + KB_ID, "SK": "SOURCE#" + sid, "sourceKind": kind, "createdAt": created,
           "ingestStatus": "ready", "lastIngestedAt": created, "pagesContributed": 2}
    row.update(extra)
    return row


def _answer(row, raw=None, truncated=False, notes=()):
    body = {"source": row, "notes": list(notes)}
    if raw is not None:
        body["raw_text"] = raw
        body["raw_truncated"] = truncated
    return {"status": 200, "body": body}


SOURCES = {
    # pasted text: no filename, a title; a source never edited carries no sourceRevision
    "aaa111": _answer(_source_row("aaa111", "pasted", "2026-11-05T10:00:00+00:00", title="Opening Hours / FAQ",
                                  filename=None), raw=PASTED_TEXT),
    # md document, edited twice on Soleon
    "bbb222": _answer(_source_row("bbb222", "md", "2026-11-05T10:01:00+00:00", title="pricing.md",
                                  filename="pricing.md", sourceRevision=2), raw=MD_TEXT),
    # md cut at the read cap
    "ccc333": _answer(_source_row("ccc333", "md", "2026-11-05T10:02:00+00:00", title="catalogue.md",
                                  filename="catalogue.md"), raw=BIG_TEXT, truncated=True,
                      notes=["raw_text truncated to 64KB — do NOT pass it back"]),
    # a web page
    "ddd444": _answer(_source_row("ddd444", "url", "2026-11-05T10:03:00+00:00", title="https://example.com/help",
                                  filename="https://example.com/help", sourceUrl="https://example.com/help",
                                  schedule="daily"),
                      notes=["raw content is only stored inline for pasted, md and txt sources (this source is 'url')."]),
    # an MCP source and one part Soleon extracted for it
    "eee555": _answer(_source_row("eee555", "mcp", "2026-11-05T10:04:00+00:00", title="Help desk",
                                  mcpConnectionId="conn-77", mcpToolAllowlist=["list_articles"], schedule="weekly"),
                      notes=["raw content is only stored inline for pasted, md and txt sources (this source is 'mcp')."]),
    "eee555-c1": _answer(_source_row("eee555-c1", "mcp_child", "2026-11-05T10:05:00+00:00", title="Article 1",
                                     parentSourceId="eee555"),
                         notes=["raw content is only stored inline for pasted, md and txt sources (this source is 'mcp_child')."]),
}

KB = {"slug": SLUG, "displayName": NAME, "kbId": KB_ID, "sourceCount": 5, "pageCount": 4, "status": "ready"}


def _page(path, *sids):
    return {"PK": "KB#" + KB_ID, "SK": "PAGE#" + path, "title": path.rsplit("/", 1)[-1], "kind": "concept",
            "contributingSourceIds": list(sids)}


PAGES = {"status": 200, "body": {"pages": [
    _page("concepts/hours.md", "aaa111"),
    _page("concepts/prices.md", "bbb222", "ccc333"),
    _page("entities/Help desk.md", "eee555-c1", "ddd444"),
    _page("index.md"),
], "total": 4, "nextCursor": None}}


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _base_dir(tmp_path: Path, folder: str = SLUG) -> Path:
    return tmp_path / ".soleon" / "knowledge-bases" / folder


def _saved_pull(tmp_path: Path, *, kb=KB, sources=SOURCES, pages=PAGES) -> Path:
    d = _base_dir(tmp_path, pkb.folder_name(kb))
    _save(d / ".pull" / "kb.json", kb)
    _save(d / ".pull" / "pages.json", pages)
    for sid, ans in sources.items():
        _save(d / ".pull" / "sources" / (sid + ".json"), ans)
    return d


@pytest.fixture()
def pulled(tmp_path):
    d = _saved_pull(tmp_path)
    summary = pkb.materialize(d, now=PULLED_AT)
    return d, summary, json.loads((d / "manifest.json").read_text(encoding="utf-8"))


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin"})


# ---------------------------------------------------------------------------
# plan: which sources to read
# ---------------------------------------------------------------------------

def test_plan_finds_sources_through_pages_then_the_mcp_parent(tmp_path):
    d = _base_dir(tmp_path)
    _save(d / ".pull" / "kb.json", KB)
    _save(d / ".pull" / "pages.json", PAGES)
    first = pkb.cmd_plan(d)
    # get_knowledge_base lists no sources: every id comes from the pages
    assert first["toFetch"] == ["aaa111", "bbb222", "ccc333", "eee555-c1", "ddd444"]
    assert first["sourceCount"] == 5 and first["pagesComplete"] is True
    _save(d / ".pull" / "sources" / "eee555-c1.json", SOURCES["eee555-c1"])
    # the extracted part names its parent only once read
    assert "eee555" in pkb.cmd_plan(d)["toFetch"]
    for sid, ans in SOURCES.items():
        _save(d / ".pull" / "sources" / (sid + ".json"), ans)
    assert pkb.cmd_plan(d)["toFetch"] == []


def test_plan_uses_a_source_list_when_the_server_sends_one(tmp_path):
    kb = dict(KB, sources=[{"sourceId": "zzz999"}, "yyy888"])
    d = _base_dir(tmp_path)
    _save(d / ".pull" / "kb.json", kb)
    _save(d / ".pull" / "pages.json", {"status": 200, "body": {"pages": [], "nextCursor": None}})
    assert pkb.cmd_plan(d)["toFetch"] == ["zzz999", "yyy888"]


def test_plan_refuses_a_folder_that_is_not_the_slug(tmp_path):
    d = _base_dir(tmp_path, "something-else")
    _save(d / ".pull" / "kb.json", KB)
    with pytest.raises(pkb.Stop, match="folder is 'demo-kb'"):
        pkb.cmd_plan(d)


def test_folder_falls_back_to_the_kb_id_for_an_unsafe_slug():
    assert pkb.folder_name({"slug": "../escape", "kbId": KB_ID}) == KB_ID
    assert pkb.folder_name({"slug": SLUG, "kbId": KB_ID}) == SLUG


def test_plan_refuses_a_source_id_that_is_not_a_file_name(tmp_path):
    pages = {"status": 200, "body": {"pages": [_page("p.md", "../../etc")], "nextCursor": None}}
    d = _base_dir(tmp_path)
    _save(d / ".pull" / "kb.json", KB)
    _save(d / ".pull" / "pages.json", pages)
    with pytest.raises(pkb.Stop, match="not a Soleon source id"):
        pkb.cmd_plan(d)


def test_materialize_refuses_until_every_source_is_read(tmp_path):
    srcs = {k: v for k, v in SOURCES.items() if k != "bbb222"}
    d = _saved_pull(tmp_path, sources=srcs)
    with pytest.raises(pkb.Stop) as exc:
        pkb.materialize(d, now=PULLED_AT)
    assert exc.value.extra["toFetch"] == ["bbb222"]
    assert not (d / "manifest.json").exists()


# ---------------------------------------------------------------------------
# materialize: files and manifest
# ---------------------------------------------------------------------------

def test_text_sources_are_written_byte_exact(pulled):
    d, summary, _m = pulled
    assert summary["files"] == ["sources/opening-hours-faq.txt", "sources/pricing.md"]
    # exact bytes: a CRLF stays a CRLF, so a push sends what Soleon holds
    assert (d / "sources/opening-hours-faq.txt").read_bytes() == PASTED_TEXT.encode("utf-8")
    assert (d / "sources/pricing.md").read_bytes() == MD_TEXT.encode("utf-8")
    # no partial file for the truncated source, no file for url / mcp
    assert sorted(p.name for p in (d / "sources").iterdir()) == ["opening-hours-faq.txt", "pricing.md"]


def test_manifest_records_every_source(pulled):
    d, summary, m = pulled
    assert (m["kbId"], m["slug"], m["name"], m["pulledAt"]) == (KB_ID, SLUG, NAME, PULLED_AT)
    by_id = {s["id"]: s for s in m["sources"]}
    assert [s["id"] for s in m["sources"]] == ["aaa111", "bbb222", "ccc333", "ddd444", "eee555"]
    pasted = by_id["aaa111"]
    assert pasted["kind"] == "pasted" and pasted["sourceRevision"] == 0  # none on Soleon = 0, as Soleon reads it
    assert pasted["localFile"] == "sources/opening-hours-faq.txt" and pasted["editable"] is True
    assert pasted["sha256"] == pkb.sha256_text(PASTED_TEXT) and pasted["bytes"] == len(PASTED_TEXT.encode())
    assert by_id["bbb222"]["sourceRevision"] == 2 and by_id["bbb222"]["title"] == "pricing.md"
    big = by_id["ccc333"]
    assert big["tooLargeToEditHere"] is True and big["localFile"] is None and big["editable"] is False
    assert "too large to edit here" in big["note"]
    url = by_id["ddd444"]
    assert (url["url"], url["schedule"], url["localFile"], url["editable"]) == (
        "https://example.com/help", "daily", None, False)
    mcp = by_id["eee555"]
    assert (mcp["connection"], mcp["schedule"], mcp["toolAllowlist"]) == ("conn-77", "weekly", ["list_articles"])
    assert mcp["childSourceIds"] == ["eee555-c1"] and mcp["localFile"] is None
    assert "eee555-c1" not in by_id  # an extracted part is listed under its source, not as one
    assert summary["remote"][0]["url"] == "https://example.com/help"
    assert summary["tooLargeToEditHere"] == ["catalogue.md"]


def test_warns_when_soleon_counts_more_sources_than_were_found(tmp_path):
    d = _saved_pull(tmp_path, kb=dict(KB, sourceCount=9))
    summary = pkb.materialize(d, now=PULLED_AT)
    assert any("Soleon counts 9 sources and 5 were found" in w for w in summary["warnings"])


def test_a_partial_page_list_is_a_warning(tmp_path):
    pages = json.loads(json.dumps(PAGES))
    pages["body"]["nextCursor"] = "abc"
    d = _saved_pull(tmp_path, pages=pages)
    assert any("part of the page list" in w for w in pkb.materialize(d, now=PULLED_AT)["warnings"])


def test_an_unreadable_source_is_a_warning_not_a_crash(tmp_path):
    srcs = dict(SOURCES, bbb222={"status": 404, "body": {"error": "source 'bbb222' not found in kb"}})
    d = _saved_pull(tmp_path, sources=srcs)
    summary = pkb.materialize(d, now=PULLED_AT)
    assert any("bbb222 could not be read (HTTP 404)" in w for w in summary["warnings"])
    assert "sources/pricing.md" not in summary["files"]


def test_claude_codes_saved_copy_of_a_long_answer_is_read(tmp_path):
    srcs = dict(SOURCES)
    srcs["bbb222"] = [{"type": "text", "text": json.dumps(SOURCES["bbb222"])}]
    d = _saved_pull(tmp_path, sources=srcs)
    pkb.materialize(d, now=PULLED_AT)
    assert (d / "sources/pricing.md").read_text(encoding="utf-8") == MD_TEXT


def test_a_re_pull_removes_the_files_the_last_pull_wrote(pulled, tmp_path):
    d, _s, _m = pulled
    (d / "sources" / "my-notes.txt").write_text("mine", encoding="utf-8")
    renamed = dict(SOURCES)
    row = dict(SOURCES["bbb222"]["body"]["source"], filename="prices-2027.md", title="prices-2027.md")
    renamed["bbb222"] = _answer(row, raw=MD_TEXT)
    _saved_pull(tmp_path, sources=renamed)
    summary = pkb.materialize(d, now="2026-11-07T08:00:00Z")
    names = sorted(p.name for p in (d / "sources").iterdir())
    assert names == ["my-notes.txt", "opening-hours-faq.txt", "prices-2027.md"]
    assert any("did not write" in w and "my-notes.txt" in w for w in summary["warnings"])


def test_same_file_names_get_the_source_id(tmp_path):
    srcs = dict(SOURCES)
    srcs["fff666"] = _answer(_source_row("fff666", "md", "2026-11-05T11:00:00+00:00", title="pricing.md",
                                         filename="Pricing.MD"), raw="# Second\n")
    pages = json.loads(json.dumps(PAGES))
    pages["body"]["pages"].append(_page("concepts/second.md", "fff666"))
    d = _saved_pull(tmp_path, sources=srcs, pages=pages)
    files = pkb.materialize(d, now=PULLED_AT)["files"]
    assert "sources/pricing.md" in files and "sources/pricing-fff666.md" in files


@pytest.mark.parametrize("name,stem", [
    ("pricing.md", "pricing"),
    ("folder/sub\\Notes v2.TXT", "notes-v2"),
    ("Québec rules — 2026", "quebec-rules-2026"),
    ("../../etc/passwd", "passwd"),
    ("CON", "source-con"),
    ("...", ""),
    ("x" * 120, "x" * 80),
])
def test_safe_file_names(name, stem):
    assert pkb.safe_stem(name) == stem


# ---------------------------------------------------------------------------
# The INDEX.md files (one document per folder, no README.md)
# ---------------------------------------------------------------------------

def test_base_index_is_the_folders_one_document(pulled):
    d, _s, m = pulled
    text = (d / "INDEX.md").read_text(encoding="utf-8")
    assert text == pkb.base_index_text(m)  # generated, deterministic
    lines = text.splitlines()
    # title, blank, exactly one header comment, then the rest
    assert lines[0] == "# " + NAME
    assert lines[1] == "" and lines[2] == HEADER
    assert sum(1 for ln in lines if ln.startswith("<!--")) == 1
    for needle in (
        "This folder holds the **sources** of a Soleon knowledge base that an agent searches: "
        "{} (`{}`).".format(NAME, KB_ID),
        "It is not the agent manager's own notes or memory.",
        "Pulled {} (UTC). It never updates by itself. Pull again before you rely on it or change it.".format(PULLED_AT),
        "pasted text, or a .md or .txt document",
        "Pushing replaces the source on Soleon and re-ingests it",
        "which costs money",
        "the change reaches every environment at once (knowledge bases have no draft)",
        "Push only when the builder says so.",
        "A web-page source refreshes itself on its schedule.",
        "(an MCP source) is extracted by Soleon",
        "`put_kb_page`). A re-ingest of its source may overwrite that edit.",
        "check them live (`list_kb_pages`, `get_kb_page`)",
        "`sources/`", "`manifest.json`",
    ):
        assert needle in text, needle
    # the explanation comes before the table
    assert text.index("A snapshot") < text.index("## Sources") < text.index("| Source |")
    rows = [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| Source")]
    assert rows == [
        "| [opening-hours-faq.txt](sources/opening-hours-faq.txt) | pasted | 0 | Opening hours are 9 to 5. |",
        "| [pricing.md](sources/pricing.md) | md | 2 | Pricing rules |",
        "| catalogue.md (too large to edit here) | md | 0 | Big catalogue |",
        "| https://example.com/help | url | 0 | (no text here: url) |",
        "| Help desk | mcp | 0 | (no text here: mcp) |",
    ]
    # the name's pipe is escaped only inside the table, never in the title
    assert lines[0].count("|") == 1


def test_no_readme_and_an_old_one_is_removed(tmp_path):
    d = _saved_pull(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text("from an earlier pull", encoding="utf-8")
    pkb.materialize(d, now=PULLED_AT)
    assert not (d / "README.md").exists()
    assert sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) == ["INDEX.md", "manifest.json", "sources"]
    assert not (d.parent / "README.md").exists()


def test_root_index_lists_every_base(pulled, tmp_path):
    other_kb = {"slug": "Not A Slug", "displayName": "Second base", "kbId": "ffee0011", "sourceCount": 1}
    other_src = {"s1": _answer(_source_row("s1", "txt", "2026-11-01T00:00:00+00:00", title="a.txt",
                                           filename="a.txt"), raw="Plain notes.")}
    other_pages = {"status": 200, "body": {"pages": [_page("p.md", "s1")], "nextCursor": None}}
    d2 = _saved_pull(tmp_path, kb=other_kb, sources=other_src, pages=other_pages)
    assert d2.name == "ffee0011"
    pkb.materialize(d2, now="2026-11-06T13:00:00Z")
    lines = (tmp_path / ".soleon/knowledge-bases/INDEX.md").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["# Knowledge bases pulled into this project", "", HEADER]
    assert sum(1 for ln in lines if ln.startswith("<!--")) == 1
    assert lines[4].startswith("Each folder here holds the sources of one Soleon knowledge base")
    items = [ln for ln in lines if ln.startswith("- ")]
    assert items == [
        "- [Demo Knowledge | Base](demo-kb/INDEX.md) (`ab12cd34ef56`), folder `demo-kb/`: "
        "pulled 2026-11-06T12:00:00Z, 5 sources (2 editable text)",
        "- [Second base](ffee0011/INDEX.md) (`ffee0011`), folder `ffee0011/`: "
        "pulled 2026-11-06T13:00:00Z, 1 source (1 editable text)",
    ]


@pytest.mark.parametrize("text,about", [
    ("# Title here\n\nBody.", "Title here"),
    ("Intro line.\n\n## Later heading ##\n", "Later heading"),
    ("No heading at all. Second sentence.", "No heading at all."),
    ("word " * 40, ("word " * 40).strip()[:99].rstrip() + "…"),
    ("   \n", "(empty)"),
])
def test_about_line(text, about):
    assert pkb.about_line(text, "md") == about


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_reports_changed_and_missing_files(pulled):
    d, _s, _m = pulled
    (d / "sources/pricing.md").write_text(MD_TEXT + "New line.\n", encoding="utf-8")
    (d / "sources/opening-hours-faq.txt").unlink()
    st = pkb.cmd_status(d)
    assert st["changed"] == ["sources/pricing.md"]
    assert st["missing"] == ["sources/opening-hours-faq.txt"]


# ---------------------------------------------------------------------------
# push-check
# ---------------------------------------------------------------------------

EDITED = MD_TEXT + "\nA new rule.\n"


def _version(ts, op, *paths, agent="admin:builder"):
    return {"ts": ts, "op": op, "agent": agent, "summary": "{} on {}".format(op, ", ".join(paths)),
            "pathsTouched": [{"path": p, "s3VersionId": "v"} for p in paths]}


def _push_answers(d, sid="bbb222", *, revision=2, raw=MD_TEXT, versions=(), pages=PAGES, truncated=False):
    row = dict(SOURCES["bbb222"]["body"]["source"]) if sid == "bbb222" else dict(SOURCES[sid]["body"]["source"])
    if revision:
        row["sourceRevision"] = revision
    else:
        row.pop("sourceRevision", None)
    pd = d / ".push" / sid
    _save(pd / "source.json", _answer(row, raw=raw, truncated=truncated))
    _save(pd / "pages.json", pages)
    _save(pd / "versions.json", {"status": 200, "body": {"versions": list(versions)}})
    return pd


@pytest.fixture()
def edited(pulled):
    d, _s, _m = pulled
    (d / "sources/pricing.md").write_bytes((EDITED).encode("utf-8"))
    return d


def test_push_check_passes_and_names_what_it_rebuilds(edited):
    pd = _push_answers(edited, versions=[
        _version(PULLED_MS + 60_000, "ingest", "concepts/prices.md", agent="ingestor"),   # ingestion: not an edit
        _version(PULLED_MS - 60_000, "admin_edit", "concepts/prices.md"),                # before the pull
        _version(PULLED_MS + 60_000, "admin_edit", "concepts/hours.md"),                 # another source's page
    ])
    out = pkb.push_check(edited, "sources/pricing.md")
    assert out["ok"] is True and out["expectedRevision"] == 2 and out["sourceId"] == "bbb222"
    assert out["pagesRebuilt"] == ["concepts/prices.md"]
    assert out["pageEditsSincePull"] == [] and out["historyReachesPull"] is True
    assert (out["linesAdded"], out["linesRemoved"]) == (2, 0)
    assert "+A new rule." in (pd / "diff.txt").read_text(encoding="utf-8")


def test_push_check_accepts_a_bare_file_name(edited):
    _push_answers(edited)
    assert pkb.push_check(edited, "pricing.md")["ok"] is True


def test_push_check_refuses_when_the_revision_moved(edited):
    _push_answers(edited, revision=3)
    with pytest.raises(pkb.Stop, match=r"revision 2 then, 3 now") as exc:
        pkb.push_check(edited, "sources/pricing.md")
    assert exc.value.code == 2 and exc.value.extra["currentRevision"] == 3


def test_a_source_with_no_revision_on_soleon_is_revision_0(pulled):
    d, _s, _m = pulled
    (d / "sources/opening-hours-faq.txt").write_bytes((PASTED_TEXT + "More.\n").encode("utf-8"))
    _push_answers(d, "aaa111", revision=0, raw=PASTED_TEXT)
    assert pkb.push_check(d, "sources/opening-hours-faq.txt")["expectedRevision"] == 0


def test_push_check_refuses_when_the_text_on_soleon_changed(edited):
    _push_answers(edited, raw=MD_TEXT + "Someone else's line.\n")
    with pytest.raises(pkb.Stop, match="text of this source on Soleon changed"):
        pkb.push_check(edited, "sources/pricing.md")


def test_push_check_stops_on_page_edits_since_the_pull(edited):
    _push_answers(edited, versions=[_version(PULLED_MS + 5_000, "admin_edit", "concepts/prices.md")])
    with pytest.raises(pkb.Stop, match="edited on Soleon since") as exc:
        pkb.push_check(edited, "sources/pricing.md")
    assert exc.value.code == 3
    (edit,) = exc.value.extra["pageEditsSincePull"]
    assert edit["pages"] == ["concepts/prices.md"] and edit["op"] == "admin_edit"
    assert edit["when"] == "2026-11-06T12:00:05Z" and edit["by"] == "admin:builder"
    # only the builder's word to overwrite them lets it go on
    assert pkb.push_check(edited, "sources/pricing.md", overwrite_page_edits=True)["ok"] is True


def test_push_check_stops_when_history_does_not_reach_the_pull(edited):
    newer = [_version(PULLED_MS + 1_000 + i, "lint", "index.md", agent="linter-bot") for i in range(200)]
    _push_answers(edited, versions=newer)
    with pytest.raises(pkb.Stop, match="does not reach back") as exc:
        pkb.push_check(edited, "sources/pricing.md")
    assert exc.value.code == 3


@pytest.mark.parametrize("file_arg,match", [
    ("sources/catalogue.md", "too large to edit here"),
    ("sources/unknown.md", "is not a source file this pull wrote"),
    ("sources/help.md", "url source: it has no editable text"),
    ("sources/opening-hours-faq.txt", "unchanged since it was pulled"),
])
def test_push_check_refusals(pulled, file_arg, match):
    d, _s, _m = pulled
    with pytest.raises(pkb.Stop, match=match):
        pkb.push_check(d, file_arg)


def test_push_check_refuses_a_web_page_or_mcp_source(pulled):
    d, _s, m = pulled
    # a hand-made file next to a url source's name is still not a pushable source
    (d / "sources" / "help-desk.md").write_text("x", encoding="utf-8")
    with pytest.raises(pkb.Stop, match="mcp source: it has no editable text"):
        pkb.push_check(d, "sources/help-desk.md")


def test_push_check_refuses_empty_and_oversized_files(pulled):
    d, _s, _m = pulled
    (d / "sources/pricing.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(pkb.Stop, match="empty"):
        pkb.push_check(d, "sources/pricing.md")
    (d / "sources/pricing.md").write_text("é" * (40 * 1024), encoding="utf-8")  # 80KB in UTF-8
    with pytest.raises(pkb.Stop, match="at most 65536"):
        pkb.push_check(d, "sources/pricing.md")


def test_push_check_refuses_when_soleon_text_is_now_too_large(edited):
    _push_answers(edited, truncated=True)
    with pytest.raises(pkb.Stop, match="over 64KB"):
        pkb.push_check(edited, "sources/pricing.md")


def test_push_check_cli_exit_codes(edited):
    _push_answers(edited, versions=[_version(PULLED_MS + 5_000, "admin_edit", "concepts/prices.md")])
    proc = _run("push-check", "--dir", str(edited), "--file", "sources/pricing.md")
    assert proc.returncode == 3, proc.stderr
    assert json.loads(proc.stdout)["ok"] is False
    proc = _run("push-check", "--dir", str(edited), "--file", "sources/pricing.md", "--overwrite-page-edits")
    assert proc.returncode == 0 and json.loads(proc.stdout)["expectedRevision"] == 2
    _push_answers(edited, revision=5)
    assert _run("push-check", "--dir", str(edited), "--file", "sources/pricing.md").returncode == 2


# ---------------------------------------------------------------------------
# record-push
# ---------------------------------------------------------------------------

def _after(d, raw, revision=3, sid="bbb222"):
    row = dict(SOURCES[sid]["body"]["source"], sourceRevision=revision, ingestStatus="queued")
    _save(d / ".push" / sid / "after.json", _answer(row, raw=raw))


def test_record_push_records_the_new_revision(edited):
    _save(edited / ".push/bbb222/result.json", {"status": 200, "body": {"source_id": "bbb222", "patched": {
        "sourceId": "bbb222", "updated": [], "reingested": True, "sourceRevision": 3}}})
    _after(edited, EDITED)
    out = pkb.record_push(edited, "sources/pricing.md", now="2026-11-06T14:00:00Z")
    assert out["ok"] is True and out["sourceRevision"] == 3 and out["reingested"] is True
    m = json.loads((edited / "manifest.json").read_text(encoding="utf-8"))
    entry = next(s for s in m["sources"] if s["id"] == "bbb222")
    assert entry["sourceRevision"] == 3 and entry["sha256"] == pkb.sha256_text(EDITED)
    assert entry["pushedAt"] == "2026-11-06T14:00:00Z"
    assert pkb.cmd_status(edited)["changed"] == []
    assert "| [pricing.md](sources/pricing.md) | md | 3 | Pricing rules |" in (edited / "INDEX.md").read_text(
        encoding="utf-8")
    # the next push measures page edits from this push, not from the pull
    (edited / "sources/pricing.md").write_bytes((EDITED + "Again.\n").encode("utf-8"))
    _push_answers(edited, revision=3, raw=EDITED,
                  versions=[_version(PULLED_MS + 60_000, "admin_edit", "concepts/prices.md")])
    assert pkb.push_check(edited, "sources/pricing.md")["pageEditsSince"] == "2026-11-06T14:00:00Z"


def test_record_push_on_a_conflict_changes_nothing(edited):
    before = (edited / "manifest.json").read_text(encoding="utf-8")
    _save(edited / ".push/bbb222/result.json", {"status": 409, "body": {
        "error": "source changed since you loaded it; reload and reapply your edits",
        "code": "SOURCE_CONFLICT", "currentRevision": 4}})
    with pytest.raises(pkb.Stop, match="someone saved this source in between \\(revision now 4\\)") as exc:
        pkb.record_push(edited, "sources/pricing.md")
    assert exc.value.code == 2
    assert (edited / "manifest.json").read_text(encoding="utf-8") == before


def test_record_push_catches_text_that_was_not_the_file(edited):
    _save(edited / ".push/bbb222/result.json", {"status": 200, "body": {"patched": {"sourceRevision": 3}}})
    _after(edited, EDITED.replace("A new rule.", "A new rul."))
    with pytest.raises(pkb.Stop, match="not the local file") as exc:
        pkb.record_push(edited, "sources/pricing.md")
    assert exc.value.code == 1
    m = json.loads((edited / "manifest.json").read_text(encoding="utf-8"))
    assert next(s for s in m["sources"] if s["id"] == "bbb222")["sourceRevision"] == 3  # can push again
    assert pkb.cmd_status(edited)["changed"] == ["sources/pricing.md"]


# ---------------------------------------------------------------------------
# the script alone
# ---------------------------------------------------------------------------

def test_cli_pull_runs_with_a_bare_path(tmp_path):
    d = _saved_pull(tmp_path)
    assert json.loads(_run("plan", "--dir", str(d)).stdout)["toFetch"] == []
    proc = _run("materialize", "--dir", str(d))
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["sources"] == 5 and summary["pulledAt"].endswith("Z")
    assert json.loads(_run("status", "--dir", str(d)).stdout)["unchanged"] == 2
