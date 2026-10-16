"""TEMPLATE: a script that lives in an agent's skill (<skill>/scripts/<name>.py) and runs with
run_code_file. Copy it, then change every line marked CHANGE.

The shape every script keeps:
  1. check_inputs() runs first and reports every problem at once. Any problem: return
     {"error": ...} before the first tool call, so a bad run costs nothing.
  2. The script keeps its own call limit, below the run form's tool_scope max_calls, and counts
     retries. At its limit (or out of time) it writes what it has and returns `state`, a small
     cursor. The platform's own cap would end the run and lose everything.
  3. Rows go straight to the destination, part by part. The return value holds counts, cost,
     a link, or an error; `state` holds a cursor, never rows.

The example: list records page by page (free), look each one up (paid), write one Google Sheet.
In the sandbox `tools` and `execution` are provided; main(inputs) is awaited once.
"""
import csv
import difflib
import io
import json
import re
import time

# ---- Must agree with the skill's run form (tool names are copied, never retyped) -------------
LIST_TOOL = "records_list"                                 # CHANGE: free, paged
LOOKUP_TOOL = "records_lookup"                             # CHANGE: paid, one call per record
CREATE_TOOL = "mcp_google-drive_import_to_google_sheets"   # CHANGE: first part makes the Sheet
APPEND_TOOL = "mcp_google-sheets_modify_sheet_values"      # CHANGE: later parts fill rows below
LOOKUP_MAX_CALLS = 700      # = this tool's max_calls in the run form (a cap per run)
OWN_LOOKUP_LIMIT = LOOKUP_MAX_CALLS - 10    # own limit per run, below the cap
TRIES = 2                   # a retry is a paid call too, so a page can cost PAGE_SIZE * TRIES
FAILS_IN_A_ROW = 5          # stop paying when the lookup tool keeps failing (an outage, a changed reply)
PRICE_PER_LOOKUP_USD = 0.01                 # CHANGE
TIME_BUDGET_S = 150         # of ~300 s wall clock (up to 90 s cold start); leave time to write
PAGE_SIZE = 50              # small enough that one page finishes well inside the time budget
MAX_ROWS = 5000                             # CHANGE: the most one list may hold
ALLOWED = {"region": ["California", "Nevada", "Oregon", "Washington"]}   # CHANGE
INPUTS = {"region", "due_before", "file_name", "state"}                  # CHANGE
COLUMNS = ["id", "name", "contact"]                                      # CHANGE
# The exact keys of a real good reply, copied from a sample. Any other shape is a failure, so an
# error that happens to carry an empty list can never read as "0 found".     CHANGE
LIST_KEYS = {"rows", "total", "has_more"}
LOOKUP_KEYS = {"email", "name", "phone"}


def check_inputs(inputs):
    """Every problem with the inputs, each naming the field and the fix. [] means go."""
    problems = []
    for key in sorted(set(inputs) - INPUTS):
        near = difflib.get_close_matches(key, sorted(INPUTS), n=1)
        problems.append(f"unknown input '{key}'" + (f" (did you mean '{near[0]}'?)" if near else "")
                        + "; allowed: " + ", ".join(sorted(INPUTS)))
    region = inputs.get("region")
    if region not in ALLOWED["region"]:
        near = difflib.get_close_matches(str(region or ""), ALLOWED["region"], n=1)
        problems.append(("region is missing" if not region else f"region '{region}' is not allowed")
                        + (f" (did you mean '{near[0]}'?)" if near else "")
                        + "; use one of: " + ", ".join(ALLOWED["region"]))
    due = inputs.get("due_before")
    if not (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)):
        problems.append("due_before must be a date like 2026-10-31")
    name = inputs.get("file_name")
    if not (isinstance(name, str) and 3 <= len(name) <= 100):
        problems.append("file_name must be a short descriptive name, e.g. overdue-california-2026-10-16")
    state = inputs.get("state")
    if state is not None and not (isinstance(state, dict) and {"page", "sheet_id", "next_row"} <= set(state)):
        problems.append("state must be copied unchanged from the last result, or left out")
    return problems


def unwrap(res):
    """Tools answer with JSON text or a dict, sometimes wrapped in {"result": ...}."""
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            return res
    if isinstance(res, dict) and isinstance(res.get("result"), (dict, list)):
        return res["result"]
    return res


def describe(res):
    """A reply's shape for an error message: its keys, never its values (they can be personal)."""
    if isinstance(res, dict):
        return "{" + ", ".join(sorted(map(str, res))) + "}"
    return type(res).__name__


def says_failed(res):
    """A reply that says it failed. Not the opposite of success: success is only ever
    confirmed by the fields the script needs (errors come in too many shapes to list)."""
    return isinstance(res, dict) and (res.get("success") is False or res.get("ok") is False)


def col(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def spent(lookups):
    return {"lookups": lookups, "cost_usd": round(lookups * PRICE_PER_LOOKUP_USD, 2)}


async def main(inputs):
    problems = check_inputs(inputs)
    if problems:
        return {"error": "nothing was run; fix this and run once more: " + "; ".join(problems),
                "tool_calls": 0, "cost_usd": 0}

    start = time.monotonic()
    state = inputs.get("state") or {"page": 1, "sheet_id": None, "next_row": 2, "lookups": 0,
                                    "failed": 0, "total": None}
    page, lookups, failed_lookups = state["page"], state.get("lookups", 0), state.get("failed", 0)
    rows, reason, more, run_lookups, fails_in_a_row = [], None, True, 0, 0

    while more and reason is None:
        # A page is done whole or not at all, and only started if its worst case, every record
        # retried, still fits under the own limit.
        if run_lookups + PAGE_SIZE * TRIES > OWN_LOOKUP_LIMIT:
            reason = "own lookup limit"
            break
        if time.monotonic() - start > TIME_BUDGET_S:
            reason = "time budget"
            break
        listed = unwrap(await tools.call(LIST_TOOL, {"region": inputs["region"],      # CHANGE args
                                                     "due_before": inputs["due_before"],
                                                     "page": page, "page_size": PAGE_SIZE}))
        # A misnamed field must never read as "nothing found", at the top or in a record.
        if (not isinstance(listed, dict) or set(listed) - LIST_KEYS or says_failed(listed)
                or not isinstance(listed.get("rows"), list)):
            return {"error": f"{LIST_TOOL} replied without a rows list (keys: "
                    + (", ".join(sorted(listed)) if isinstance(listed, dict) else type(listed).__name__)
                    + "); nothing more was spent", **spent(lookups)}
        batch = listed["rows"]
        bad = [r for r in batch if not (isinstance(r, dict) and {"id", "name"} <= set(r))]   # CHANGE
        if bad:
            return {"error": f"{LIST_TOOL} records lack id or name (keys: "
                    + ", ".join(sorted(bad[0])) + "); nothing more was spent", **spent(lookups)}
        if state.get("total") is None:
            state["total"] = min(int(listed.get("total") or len(batch)), MAX_ROWS)
        for record in batch:
            found, reply = None, None
            for _ in range(TRIES):      # a retry is a paid call too: counted
                lookups += 1
                run_lookups += 1
                try:
                    reply = unwrap(await tools.call(LOOKUP_TOOL, {"id": record["id"]}))   # CHANGE
                except Exception as exc:
                    reply = str(exc)
                    continue
                # A find is confirmed only by the field we need, with a value.     CHANGE: your fields
                if isinstance(reply, dict) and not set(reply) - LOOKUP_KEYS and reply.get("email"):
                    found = reply
                    break
            if found is None:
                failed_lookups += 1
                fails_in_a_row += 1
                if fails_in_a_row >= FAILS_IN_A_ROW:
                    return {"error": f"{LOOKUP_TOOL} failed for {fails_in_a_row} records in a row; "
                            f"stopped paying. Last reply's keys: {describe(reply)}. Run again with this state "
                            f"once the tool works (this part's {run_lookups} lookups will be paid again)",
                            **spent(lookups), "state": dict(inputs.get("state") or state)}
            else:
                fails_in_a_row = 0
            rows.append([str(record["id"]), record.get("name") or "",                     # CHANGE
                         (found or {}).get("email") or ""])
        more = bool(listed.get("has_more")) and bool(batch) and state["next_row"] - 2 + len(rows) < MAX_ROWS
        page += 1

    # Write this run's part to the destination here, so no row reaches the model. If a write
    # fails, return the state from BEFORE this part: running again redoes just this part.
    before = dict(inputs.get("state") or state)

    def write_failed(what):
        return {"error": f"{what}; run again with this state to redo this part "
                f"(its {run_lookups} lookups will be paid again)", **spent(lookups), "state": before}

    if rows and state["sheet_id"] is None:
        # A new Google Sheet has about 1,000 rows of grid; pad the first import with blank rows
        # for the whole list so later parts fit.
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(COLUMNS)
        w.writerows(rows)
        out.write(("," * (len(COLUMNS) - 1) + "\n") * max(0, state["total"] - len(rows)))
        try:
            made = unwrap(await tools.call(CREATE_TOOL, {"file_name": inputs["file_name"],
                                                         "content": out.getvalue(), "source_format": "csv"}))
        except Exception as exc:
            return write_failed(f"the Sheet was not created: {type(exc).__name__}")
        # Created only if the reply holds the new Sheet's link.
        m = None if says_failed(made) else re.search(r"spreadsheets/d/([A-Za-z0-9_-]+)", json.dumps(made))
        if not m:
            return write_failed("the Sheet was not created (no link came back)")
        state["sheet_id"] = m.group(1)
    elif rows:
        # Text cells only, sent as USER_ENTERED; a leading ' keeps zips and ids exactly.
        values = [["'" + c if c else "" for c in r] for r in rows]
        first = state["next_row"]
        try:
            wrote = unwrap(await tools.call(APPEND_TOOL, {
                "spreadsheet_id": state["sheet_id"],
                "range_name": f"A{first}:{col(len(COLUMNS))}{first + len(rows) - 1}",
                "values": json.dumps(values), "value_input_option": "USER_ENTERED"}))
        except Exception as exc:
            return write_failed(f"rows {first}-{first + len(rows) - 1} were not written: {type(exc).__name__}")
        # Written only if the reply confirms every row.   CHANGE: match your write tool's success reply
        if not (isinstance(wrote, dict) and wrote.get("updatedRows") == len(rows)):
            return write_failed(f"rows {first}-{first + len(rows) - 1} were not confirmed as written: "
                                f"reply {describe(wrote)}")
    state["next_row"] += len(rows)
    state.update(page=page, lookups=lookups, failed=failed_lookups)

    summary = {"rows": state["next_row"] - 2, "failed_lookups": failed_lookups, **spent(lookups),
               "link": (f"https://docs.google.com/spreadsheets/d/{state['sheet_id']}/edit"
                        if state["sheet_id"] else None)}
    if reason and more:
        return {"done": False, "reason": reason, **summary, "state": state,
                "next": "run again at once with the same inputs plus this state"}
    return {"done": True, **summary}
