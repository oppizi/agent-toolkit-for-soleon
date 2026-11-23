"""Waiving a local extraction job's token budget, with the person's approval,
BEFORE the job starts (Dan 2026-11-22).

Asked at 85% instead (#71-#73), the approval never reached him: an
extraction's helpers do the reading, spend the budget and cannot ask anyone,
and the agent, told the budget is spent, writes instead of reading on. Two
refreshes that day each ended short with no prompt shown."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_kb_local_ingest as kli  # noqa: E402
import soleon_kb_refresh as kr  # noqa: E402
import soleon_message_budget as mb  # noqa: E402

CALL = "lc_" + "1" * 24
JOB = "ext_" + "2" * 32 + "_"
KB = "2ebb3c747a88"
#: before the fixture moves it
REFRESH_STATE_DIR = kr.STATE_DIR
PREP = ('python "C:/p/bin/soleon_newest.py" "C:/p/bin/soleon_kb_local_ingest.py" prepare-extractor '
        '--kb {} --call {} --job {} --waive-budget'.format(KB, CALL, JOB))
INSTALL = 'python "C:/p/bin/soleon_kb_refresh.py" install --kb {} --source s1 --project C:/x --waive-budget'.format(KB)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "WAIVER_ASK_FILE", tmp_path / "asks" / "budget-waiver-ask.json")
    monkeypatch.setattr(kr, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv(kli.WAIVER_ENV, raising=False)


def _gate(command, tool="Bash", mode="bypassPermissions", agent_id=None, capsys=None):
    hook = {"tool_name": tool, "tool_input": {"command": command}, "permission_mode": mode,
            "hook_event_name": "PreToolUse"}
    if agent_id:
        hook["agent_id"] = agent_id
    rc = mb.main(["agent-pretool"], stream=io.StringIO(json.dumps(hook)))
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out)["hookSpecificOutput"] if out else None)


# --- the approval prompt ------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_a_waiver_command_goes_to_the_persons_approval_prompt_even_in_bypass_mode(tool, capsys):
    rc, decision = _gate(PREP, tool=tool, capsys=capsys)
    assert rc == 0 and decision["permissionDecision"] == "ask"
    assert "this knowledge-base extraction job" in decision["permissionDecisionReason"]
    assert "without --waive-budget" in decision["permissionDecisionReason"]


def test_the_daily_tasks_waiver_says_it_lasts_every_day(capsys):
    _, decision = _gate(INSTALL, capsys=capsys)
    assert decision["permissionDecision"] == "ask"
    assert "every daily refresh this task runs" in decision["permissionDecisionReason"]


def test_setting_the_waiver_by_hand_is_asked_about_too(capsys):
    _, decision = _gate("{}=1 ".format(kli.WAIVER_ENV) + PREP.replace(" --waive-budget", ""), capsys=capsys)
    assert decision["permissionDecision"] == "ask"


def test_with_no_one_to_ask_a_waiver_is_refused(capsys):
    _, decision = _gate(PREP, mode="dontAsk", capsys=capsys)
    assert decision["permissionDecision"] == "deny"
    assert not mb.take_waiver_ask("prepare-extractor")


@pytest.mark.parametrize("command", [
    'python soleon_kb_local_ingest.py prepare-extractor --kb x --call c --job j',   # no waiver
    'grep -rn "waive-budget" plugins/',                                              # only mentions it
    'python soleon_kb_local_ingest.py run --kb x --call c',
])
def test_other_commands_are_untouched(command, capsys):
    assert _gate(command, capsys=capsys) == (0, None)


def test_inside_an_agent_run_the_gate_stands_aside(capsys, tmp_path):
    """A subagent's own call is the 85% budget's business, not the waiver gate's."""
    assert mb.waiver_gate({"tool_name": "Bash", "agent_id": "a1", "tool_input": {"command": PREP}}) is False


def test_the_plugin_hook_lets_waiver_commands_reach_the_gate():
    """Its shell filter used to pass only subagents' calls: the main
    conversation's commands never reached Python."""
    hooks = json.loads((BIN.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    pre = [h["command"] for e in hooks["PreToolUse"] if not e.get("matcher") for h in e["hooks"]
           if "agent-pretool" in h["command"]]
    assert len(pre) == 1 and "*waive-budget*" in pre[0] and "*SOLEON_KB_BUDGET_WAIVED*" in pre[0]


# --- the command honours only a prompt that was shown ---------------------------------

def _pulled_extractor(project: Path) -> Path:
    agent_dir = project / ".soleon" / "agents" / "kb-extractor"
    agent_dir.mkdir(parents=True)
    (agent_dir / "pull.json").write_text(json.dumps({"slug": "kb-extractor", "model": "sonnet"}))
    (agent_dir / "config.json").write_text(json.dumps({"loop": {"tokenBudget": 500000}}))
    return agent_dir


class _Client:
    def call_tool(self, name, args):
        if name == "list_agent_tools":
            return {"state": "done", "call_id": CALL, "result": {"tools": [{"name": "emit_document"}]}}
        raise AssertionError("a waived job reads no budget: " + name)


def _prepare(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(kli, "await_control", lambda c, slug, env, **k: env)
    return kli.prepare_extractor(_Client(), tmp_path, CALL, JOB, kb_id=KB,
                                 runner=lambda cmd, **k: SimpleNamespace(returncode=0), **kw)


def test_an_approved_waiver_takes_the_budget_off_the_agent_and_its_helpers(tmp_path, monkeypatch, capsys):
    agent_dir = _pulled_extractor(tmp_path)
    _gate(PREP, capsys=capsys)                       # the prompt was shown; the person approved
    assert kli.budget_waived(True, KB) is True
    _prepare(tmp_path, monkeypatch, waive_budget=True)
    assert json.loads((agent_dir / kli.EXTRACTION_FILE).read_text())["budgetWaived"] is True
    assert mb.budget_of(agent_dir) == 0              # 0 = no budget: the agent's hook and helpers' pass
    assert kli.budget_waived(False, KB) is False


def test_a_waiver_whose_prompt_was_never_shown_is_refused():
    """A conversation that started before the gate existed runs the old hook
    list: the flag would pass with no prompt at all. The command refuses."""
    with pytest.raises(kli.SoleonClientError, match="was not approved"):
        kli.budget_waived(True, KB)


def test_one_approval_waives_one_job():
    mb.write_waiver_ask(PREP)
    assert kli.budget_waived(True, KB) is True
    with pytest.raises(kli.SoleonClientError):
        kli.budget_waived(True, KB)


def test_a_prompt_shown_for_another_job_approves_nothing_here():
    """A refused prompt leaves its record behind: it must not approve the next job."""
    mb.write_waiver_ask(PREP)
    with pytest.raises(kli.SoleonClientError):
        kli.budget_waived(True, KB, job_id="ext_" + "9" * 32 + "_")
    assert kli.budget_waived(True, KB, job_id=JOB) is True


def test_an_install_prompt_for_another_knowledge_base_approves_nothing_here(tmp_path):
    mb.write_waiver_ask(INSTALL.replace(KB, "ffffffffffff"))
    with pytest.raises(SystemExit, match="was not approved"):
        kr.install(KB, ["s1"], tmp_path, "06:30", runner=_Runner(), platform="win32", waive_budget=True)


def test_an_install_approval_does_not_waive_a_job():
    mb.write_waiver_ask(INSTALL)
    with pytest.raises(kli.SoleonClientError):
        kli.budget_waived(True, KB)


def test_an_old_prompt_record_is_not_an_approval():
    mb.write_waiver_ask(PREP)
    assert mb.take_waiver_ask("prepare-extractor", now=10 ** 12) is False


# --- the daily task -----------------------------------------------------------------

class _Runner:
    def __init__(self, output="REFRESH OK\n"):
        self.output, self.calls = output, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if hasattr(kw.get("stdout"), "write"):
            kw["stdout"].write(self.output)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_an_approved_install_hands_every_daily_run_the_waiver(tmp_path, capsys):
    _gate(INSTALL, capsys=capsys)
    kr.install(KB, ["s1"], tmp_path, "06:30", runner=_Runner(), platform="win32", waive_budget=True)
    wrapper = (kr.STATE_DIR / "{}.cmd".format(KB)).read_text(encoding="utf-8")
    assert "--waive-budget" in wrapper
    runner = _Runner()
    assert kr.run(KB, ["s1"], tmp_path, runner=runner, claude="claude", waive_budget=True) == 0
    env = runner.calls[0][1]["env"]
    assert env[kli.WAIVER_ENV] == "1"
    assert kli.budget_waived(False, KB, environ=env,                # the headless prepare-extractor
                             standing=lambda kb: mb.standing_waiver(kb, kr.STATE_DIR)) is True
    assert json.loads(kr.status_path(KB).read_text())["budgetWaived"] is True
    assert [r["kb"] for r in kr.last_runs(is_on=lambda _kb: True)] == [KB]  # the waiver record is not a run


def test_an_install_whose_prompt_was_never_shown_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="was not approved"):
        kr.install(KB, ["s1"], tmp_path, "06:30", runner=_Runner(), platform="win32", waive_budget=True)
    assert not mb.standing_waiver(KB, kr.STATE_DIR).exists()


def test_reinstalling_without_the_waiver_ends_it(tmp_path, capsys):
    _gate(INSTALL, capsys=capsys)
    kr.install(KB, ["s1"], tmp_path, "06:30", runner=_Runner(), platform="win32", waive_budget=True)
    kr.install(KB, ["s1"], tmp_path, "06:30", runner=_Runner(), platform="win32")
    runner = _Runner()
    kr.run(KB, ["s1"], tmp_path, runner=runner, claude="claude", waive_budget=True)  # an old wrapper
    assert kli.WAIVER_ENV not in runner.calls[0][1]["env"]
    with pytest.raises(kli.SoleonClientError):
        kli.budget_waived(False, KB, environ={kli.WAIVER_ENV: "1"},
                          standing=lambda kb: mb.standing_waiver(kb, kr.STATE_DIR))


def test_a_run_without_the_waiver_never_passes_one_on(tmp_path, monkeypatch):
    monkeypatch.setenv(kli.WAIVER_ENV, "1")              # inherited from somewhere else
    runner = _Runner()
    kr.run(KB, ["s1"], tmp_path, runner=runner, claude="claude")
    assert kli.WAIVER_ENV not in runner.calls[0][1]["env"]


def test_the_ingest_script_looks_for_the_daily_waiver_where_the_task_writes_it():
    assert mb.standing_waiver(KB).parent == REFRESH_STATE_DIR
