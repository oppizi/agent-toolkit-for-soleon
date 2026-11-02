"""Example fakes for run_locally.py and script_template.py: made-up records, a paid lookup that
sometimes fails, and a fake Google Sheet that keeps Google's rules (1,000-row grid on a new
Sheet, text-only cells on writes). Copy it and make each fake behave like your real tool."""
import csv
import io
import json
import re

TOTAL = 2500                 # records the list tool will page through
SHEETS = {}
_lookups = [0]


async def handle(name, args):
    if name == "records_list":
        page, size = args["page"], args["page_size"]
        ids = range((page - 1) * size + 1, min(page * size, TOTAL) + 1)
        return {"result": {"rows": [{"id": i, "name": "Customer %d" % i} for i in ids],
                           "total": TOTAL, "has_more": page * size < TOTAL}}
    if name == "records_lookup":
        _lookups[0] += 1
        if _lookups[0] % 97 == 0:
            raise RuntimeError("fake transient error")
        return json.dumps({"email": "c%d@example.com" % args["id"]})
    if name == "mcp_google-drive_import_to_google_sheets":
        rows = list(csv.reader(io.StringIO(args["content"])))
        sid = "fake%d" % (len(SHEETS) + 1)
        SHEETS[sid] = {"grid": max(1000, len(rows)), "rows": {i + 1: r for i, r in enumerate(rows)}}
        return "Imported. https://docs.google.com/spreadsheets/d/%s/edit" % sid
    if name == "mcp_google-sheets_modify_sheet_values":
        sheet = SHEETS[args["spreadsheet_id"]]
        top, bottom = map(int, re.findall(r"\d+", args["range_name"]))
        if bottom > sheet["grid"]:
            raise RuntimeError("Range exceeds grid limits. Max rows: %d" % sheet["grid"])
        values = json.loads(args["values"])
        if not all(isinstance(c, str) for r in values for c in r):
            raise RuntimeError("Tool arguments were rejected by effect policy")
        for i, r in enumerate(values):
            sheet["rows"][top + i] = [c[1:] if c.startswith("'") else c for c in r]
        return {"updatedRows": len(values)}
    raise KeyError(name)
