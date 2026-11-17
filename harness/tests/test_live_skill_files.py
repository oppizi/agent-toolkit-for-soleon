"""Skill package files the pull takes from the LIVE copy (2026-11-17).

A draft lists a file it keeps unchanged by path only (no text), and a published read leaves out a file
over 64 KB, so their text comes from get_agent_skills(content_mode="files"). The pull stops when a text
file has no text anywhere, and a save sends a still-unchanged live file back as a KEEP entry (path only)
so the platform's 128 KB-per-agent inline cap counts only what was edited."""
from __future__ import annotations

import json
import sys

import pytest

from _local_emulation_fixtures import BIN, PULL_ASSETS

sys.path.insert(0, str(BIN))
sys.path.insert(0, str(PULL_ASSETS))
import pull_agent  # noqa: E402
import soleon_agent_document as doc  # noqa: E402

SKILL = {"id": "builder", "name": "builder", "description": "", "content": "# B", "enabled": True,
         "files": [{"path": "references/1-brief.md", "enabled": True},                       # kept: path only
                   {"path": "references/2-brand.md", "enabled": True, "content": "draft edit"},  # in the draft
                   {"path": "engine/big.py", "enabled": True, "size": 98000, "mime": "text/x-python"},
                   {"path": "assets/logo.png", "enabled": True, "size": 10, "mime": "image/png"}]}
LIVE = {"builder": {"references/1-brief.md": "the brief", "references/2-brand.md": "live brand",
                    "engine/big.py": "x = 1\n", "assets/logo.png": None}}


def _skills_answer(live):
    return {"slug": "a", "skills": [{"name": sid, "enabled": True, "files": sorted(texts), "fileContents": texts}
                                    for sid, texts in live.items()]}


def test_the_answer_shape_is_read_with_or_without_the_body_envelope():
    assert pull_agent._live_file_texts(_skills_answer(LIVE)) == LIVE
    assert pull_agent._live_file_texts({"status": 200, "body": _skills_answer(LIVE)}) == LIVE
    full_mode = {"skills": [{"name": "builder", "files": ["SKILL.md"], "content": "# B"}]}
    assert pull_agent._live_file_texts(full_mode) is None


def test_kept_and_oversize_files_take_the_live_text_and_draft_edits_win():
    (skill,) = pull_agent.fill_live_files([SKILL], LIVE, has_draft=True)
    files = {f["path"]: f for f in skill["files"]}
    assert files["references/1-brief.md"]["content"] == "the brief" and files["references/1-brief.md"][doc.LIVE_MARK]
    assert files["engine/big.py"]["content"] == "x = 1\n" and files["engine/big.py"][doc.LIVE_MARK]
    assert files["references/2-brand.md"]["content"] == "draft edit" and doc.LIVE_MARK not in files["references/2-brand.md"]
    assert "content" not in files["assets/logo.png"]
    assert "content" not in SKILL["files"][0]  # the pulled document itself is never changed


def test_a_published_read_marks_its_inline_text_as_live():
    published = {**SKILL, "files": [{"path": "references/a.md", "enabled": True, "content": "live a"}]}
    (skill,) = pull_agent.fill_live_files([published], {"builder": {}}, has_draft=False)
    assert skill["files"][0][doc.LIVE_MARK] is True


def test_a_text_file_with_no_text_anywhere_stops_the_pull():
    with pytest.raises(SystemExit) as exc:
        pull_agent.fill_live_files([SKILL], None, has_draft=True)
    message = str(exc.value)
    assert "builder/references/1-brief.md" in message and "builder/engine/big.py" in message
    assert 'content_mode=\\"files\\"' not in message and 'content_mode="files"' in message
    assert "assets/logo.png" not in message  # binary files are never written locally


def test_a_save_keeps_unchanged_live_files_and_sends_edited_ones(tmp_path):
    filled = pull_agent.fill_live_files([SKILL], LIVE, has_draft=True)
    doc.write_skills(tmp_path, filled)
    meta = json.loads((tmp_path / "skills/builder/skill.json").read_text(encoding="utf-8"))
    assert set(meta[doc.LIVE_FILES_KEY]) == {"references/1-brief.md", "engine/big.py"}
    (tmp_path / "skills/builder/engine/big.py").write_text("x = 2\n", encoding="utf-8")  # edited locally
    (rebuilt,) = doc.read_skills(tmp_path, [SKILL])
    files = {f["path"]: f for f in rebuilt["files"]}
    assert files["references/1-brief.md"] == {"path": "references/1-brief.md", "enabled": True}
    assert files["engine/big.py"]["content"] == "x = 2\n"
    assert files["references/2-brand.md"]["content"] == "draft edit"
    assert files["assets/logo.png"] == {"path": "assets/logo.png", "enabled": True, "size": 10, "mime": "image/png"}
