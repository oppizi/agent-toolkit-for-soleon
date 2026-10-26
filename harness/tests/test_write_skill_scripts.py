"""write-skill-scripts: the template script keeps the skill's rules, and the local runner keeps
the platform's.

The template is run by the runner it ships with, against the example fakes it ships with, so a
change to any of the three that breaks a rule fails here: validation before the first call, no
rows in the return value, never reaching the platform cap, resuming into one Sheet.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "plugins" / "soleon" / "skills" / "write-skill-scripts" / "assets"
SCRIPT = ASSETS / "script_template.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ASSETS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner():
    return _load("run_locally")


@pytest.fixture
def fakes():
    return _load("example_fakes")


@pytest.fixture
def form(runner):
    data = json.loads((ASSETS / "example_form.json").read_text(encoding="utf-8"))
    # A fresh time: the platform refuses request ids older than 30 days.
    from datetime import datetime, timezone
    data["request_id"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "/test"
    return data


def test_example_form_is_accepted(runner, form):
    assert runner.check_form(form) == []


@pytest.mark.parametrize("mutate, expected", [
    (lambda f: f["tool_scope"][0].pop("constraints"), '"constraints": {}'),
    (lambda f: f.update(request_id="overdue-list"), "request_id must be"),
    (lambda f: f.update(request_id="2020-01-01T00:00:00Z/old"), "older than 30 days"),
    (lambda f: f["tool_scope"][1].update(max_calls=900), "above limits.max_tool_calls"),
    (lambda f: f["limits"].update(max_tool_calls=2121), "from 0 to 1000"),
    (lambda f: f["output_scope"].pop("deliver"), "attached to the chat"),
    (lambda f: f.pop("output_scope"), "output_scope is required"),
    (lambda f: f.update(file="build_list.py"), "path inside the skill"),
])
def test_check_form_catches_each_mistake(runner, form, mutate, expected):
    mutate(form)
    problems = runner.check_form(form)
    assert any(expected in p for p in problems), problems


def test_bad_inputs_cost_nothing_and_name_the_fix(runner, form, fakes):
    form["inputs"] = {"region": "Califonia", "due_befor": "2026-10-31", "file_name": "x-list"}
    res = runner.run(SCRIPT, form, fakes.handle)
    assert res.calls == [] and res.aborted is None
    error = res.value["error"]
    assert "did you mean 'California'" in error
    assert "did you mean 'due_before'" in error
    assert "due_before must be a date" in error


def test_big_job_resumes_into_one_sheet_and_never_hits_the_cap(runner, form, fakes):
    runs, res = 0, None
    while True:
        runs += 1
        res = runner.run(SCRIPT, form, fakes.handle)
        assert res.aborted is None, res.aborted
        assert not [p for p in res.problems if "return value" in p], res.problems
        assert len(json.dumps(res.value)) < 2000, "the return value should be a short summary"
        if res.value["done"]:
            break
        assert len(json.dumps(res.value["state"])) < 500, "state is a cursor, never the rows"
        form = copy.deepcopy(form)
        form["inputs"]["state"] = res.value["state"]
        assert runs < 50, "never finished"
    assert runs > 1, "the example must need a resume"
    assert len(fakes.SHEETS) == 1, "a resumed run must write into the same Sheet"
    sheet = next(iter(fakes.SHEETS.values()))["rows"]
    data = [r for n, r in sheet.items() if n > 1 and any(r)]
    assert len(data) == fakes.TOTAL == res.value["rows"]
    assert len({r[0] for r in data}) == fakes.TOTAL, "no record twice"


def test_the_cap_ends_the_run_and_loses_the_return(runner, form, fakes):
    # A form whose cap is below the script's own limit: the platform cuts the run.
    form["tool_scope"][1]["max_calls"] = 50
    res = runner.run(SCRIPT, form, fakes.handle)
    assert res.value is None
    assert "Tool call budget reached" in res.aborted


def test_a_tool_outside_tool_scope_is_refused(runner, form, fakes):
    form["tool_scope"] = [e for e in form["tool_scope"] if e["tool"] != "records_list"]
    res = runner.run(SCRIPT, form, fakes.handle)
    assert "not in this run's tool_scope" in (res.aborted or "")


def test_rows_in_the_return_value_are_flagged(runner, form, tmp_path):
    script = tmp_path / "leaky.py"
    script.write_text("rows = [{'id': i} for i in range(50)]\nreturn {'rows': rows}\n", encoding="utf-8")
    form["tool_scope"] = []
    form["limits"]["max_tool_calls"] = 0

    async def handle(name, args):
        raise AssertionError("no tool calls expected")

    res = runner.run(script, form, handle)
    assert any("carries rows" in p for p in res.problems)


def test_a_return_over_64_kib_fails_the_run(runner, form, tmp_path):
    script = tmp_path / "big.py"
    script.write_text("return {'blob': 'x' * 70000}\n", encoding="utf-8")
    res = runner.run(script, form, None)
    assert res.value is None and "64 KiB" in res.aborted


def test_a_reply_without_the_expected_field_is_an_error_not_an_empty_list(runner, form, fakes):
    async def renamed(name, args):
        if name == "records_list":
            return {"result": {"items": [], "total": 0}}
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, renamed)
    assert "replied without a rows list" in res.value["error"]
    assert res.value.get("rows") is None


def _until_done(runner, form, handle, limit=60):
    runs, res = 0, None
    while True:
        runs += 1
        res = runner.run(SCRIPT, form, handle)
        assert res.aborted is None, res.aborted
        if res.value.get("done") or "error" in res.value or "state" not in res.value:
            return res, runs
        form = copy.deepcopy(form)
        form["inputs"]["state"] = res.value["state"]
        assert runs < limit, "never finished"


@pytest.mark.parametrize("reply", ["Error: quota exceeded", {"status": "error"}, {"ok": False}])
def test_a_failed_write_returns_the_state_from_before_the_part(runner, form, fakes, reply):
    writes = [0]

    async def flaky(name, args):
        if name == "mcp_google-sheets_modify_sheet_values":
            writes[0] += 1
            if writes[0] == 2:
                return reply
        return await fakes.handle(name, args)

    res, _ = _until_done(runner, form, flaky)
    assert "written" in res.value["error"]
    retry = copy.deepcopy(form)
    retry["inputs"]["state"] = res.value["state"]
    res, _ = _until_done(runner, retry, fakes.handle)
    assert res.value["done"] and res.value["rows"] == fakes.TOTAL
    assert len(fakes.SHEETS) == 1
    sheet = next(iter(fakes.SHEETS.values()))["rows"]
    assert len({r[0] for n, r in sheet.items() if n > 1 and any(r)}) == fakes.TOTAL


def test_the_first_part_failing_can_be_redone(runner, form, fakes):
    async def no_sheet(name, args):
        if name == "mcp_google-drive_import_to_google_sheets":
            raise RuntimeError("Drive is down")
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, no_sheet)
    assert "not created" in res.value["error"] and res.value["state"]["sheet_id"] is None
    retry = copy.deepcopy(form)
    retry["inputs"]["state"] = res.value["state"]
    res, _ = _until_done(runner, retry, fakes.handle)
    assert res.value["done"] and res.value["rows"] == fakes.TOTAL


def test_a_field_renamed_inside_each_record_is_an_error(runner, form, fakes):
    async def renamed(name, args):
        reply = await fakes.handle(name, args)
        if name == "records_list":
            reply["result"]["rows"] = [{"record_id": r["id"], "name": r["name"]} for r in reply["result"]["rows"]]
        if name == "records_lookup":
            return {"mail": "x@example.com"}
        return reply

    res = runner.run(SCRIPT, form, renamed)
    assert "lack id or name" in res.value["error"]

    async def lookup_renamed(name, args):
        if name == "records_lookup":
            return {"mail": "x@example.com"}
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, lookup_renamed)
    assert "5 records in a row" in res.value["error"] and "mail" in res.value["error"]
    assert res.value["lookups"] == 10, "a changed reply stops paying after a few records"


FAILURES = [{"error": "not found"}, {"status": "error"}, "Error: not found", "HttpError 403",
            "Tool execution failed: timeout", {"success": False}, {"ok": False, "email": "x@y.z"},
            {"email": ""}, {"email": None}, None, {"status": "error", "email": None, "phone": None},
            {"email": "x@example.com", "message": "rate limited"}]


@pytest.mark.parametrize("reply", FAILURES)
def test_a_failure_reply_is_never_a_find(runner, form, fakes, reply):
    async def every_other(name, args):
        if name == "records_lookup":
            if args["id"] % 2:          # every other record, so the run never stops for failures in a row
                return {"email": "ok@example.com"}
            return reply
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, every_other)
    assert "error" not in res.value
    assert 0 < res.value["failed_lookups"] < res.value["rows"]
    sheet = next(iter(fakes.SHEETS.values()))["rows"]
    blank = sum(1 for n, r in sheet.items() if n > 1 and r and r[0] and not r[2])
    assert blank == res.value["failed_lookups"], "every failure is counted, none is a find"


@pytest.mark.parametrize("reply", FAILURES)
def test_a_lookup_outage_stops_paying(runner, form, fakes, reply):
    async def down(name, args):
        if name == "records_lookup":
            return reply
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, down)
    assert "in a row" in res.value["error"] and res.value["lookups"] == 10
    assert res.value["state"]["page"] == 1, "nothing was written, so the resume redoes the part"


@pytest.mark.parametrize("reply", ["ok", "HttpError 403: forbidden", "Successfully updated",
                                   {"success": False}, {"updatedRows": 0}, None])
def test_a_write_counts_only_when_its_reply_confirms_it(runner, form, fakes, reply):
    async def unconfirmed(name, args):
        if name == "mcp_google-sheets_modify_sheet_values":
            await fakes.handle(name, args)
            return reply
        return await fakes.handle(name, args)

    res, _ = _until_done(runner, form, unconfirmed)
    assert "not confirmed as written" in res.value["error"] and "state" in res.value


@pytest.mark.parametrize("reply", [{"success": False, "rows": []}, {"status": "error", "rows": []},
                                   {"rows": [], "error": None, "total": 0}, {"rows": [], "message": "denied"}])
def test_an_empty_list_in_an_unknown_shape_is_not_zero_found(runner, form, fakes, reply):
    async def refused(name, args):
        if name == "records_list":
            return reply
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, refused)
    assert "error" in res.value and len(res.calls) == 1


def test_error_messages_carry_keys_not_data(runner, form, fakes):
    async def leaky(name, args):
        if name == "records_lookup":
            return {"status": "error", "email": "jane.private@example.com"}
        return await fakes.handle(name, args)

    res = runner.run(SCRIPT, form, leaky)
    assert "in a row" in res.value["error"] and "jane.private" not in json.dumps(res.value)


def test_need_refuses_a_missing_field_instead_of_a_blank_cell(runner):
    template = _load("script_template")
    assert template.need({"amount": 0}, "amount") == 0
    with pytest.raises(template.Missing):
        template.need({"amt": 5}, "amount")
