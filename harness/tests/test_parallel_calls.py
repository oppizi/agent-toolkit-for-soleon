"""bin/soleon_parallel.py and its use by the two stdio servers: a step's calls run together, as on Soleon
(agent-infra loop.py bounded_gather + _in_file_order), at most the agent's fan-out at once; a file call waits for an
earlier write of its path; Claude Code is told it may send them together (readOnlyHint), except an approval-gated
tool; and what the calls share (the trackers, the long-result pages, the token) stays consistent."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_mcp_client as client_mod  # noqa: E402
import soleon_parallel as par  # noqa: E402
import soleon_workspace_mcp as ws_mod  # noqa: E402


def _collector():
    out, lock = [], threading.Lock()

    def write(resp):
        with lock:
            out.append((time.monotonic(), resp))
    return out, write


def _msg(i, name="slow", **arguments):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_calls_run_together_up_to_the_limit():
    out, write = _collector()
    calls = par.ParallelCalls(write, limit=2)
    running, peak, lock = [0], [0], threading.Lock()

    def handle(msg):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    t0 = time.monotonic()
    for i in range(4):
        calls.submit(_msg(i), handle)
    calls.join()
    took = time.monotonic() - t0
    assert sorted(r["id"] for _t, r in out) == [0, 1, 2, 3]
    assert peak[0] == 2  # the limit holds
    assert 0.55 < took < 1.1  # two at a time, not one (1.2 s) and not four (0.3 s)


def test_a_read_waits_for_an_earlier_write_of_its_path_and_other_paths_do_not():
    out, write = _collector()
    calls = par.ParallelCalls(write, limit=4)
    log, lock = [], threading.Lock()

    def handle(msg):
        p = msg["params"]
        if p["name"] == "write_file":
            time.sleep(0.3)
        with lock:
            log.append((p["name"], p["arguments"]["path"], time.monotonic()))
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    calls.submit(_msg(1, "write_file", path="./notes/a.json", content="x"), handle)
    calls.submit(_msg(2, "read_file", path="notes/a.json"), handle)   # same path, spelled differently
    calls.submit(_msg(3, "read_file", path="notes/b.json"), handle)   # another path
    calls.join()
    order = [(n, p) for n, p, _t in log]
    assert order.index(("read_file", "notes/a.json")) > order.index(("write_file", "./notes/a.json"))
    assert order[0] == ("read_file", "notes/b.json")  # never held back


def test_a_failing_call_answers_with_an_error_and_the_rest_carry_on():
    out, write = _collector()
    calls = par.ParallelCalls(write, limit=4)

    def handle(msg):
        if msg["id"] == 1:
            raise RuntimeError("boom")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}

    calls.submit(_msg(1), handle)
    calls.submit(_msg(2), handle)
    calls.join()
    by = {r["id"]: r for _t, r in out}
    assert by[1]["error"]["code"] == -32603 and "boom" in by[1]["error"]["message"]
    assert by[2]["result"] == {"ok": True}


@pytest.mark.parametrize("effort,expected", [
    ({"default": "balanced", "ceiling": "balanced"}, 4),
    ({"default": "swift"}, 2),
    ({"default": "exhaustive", "ceiling": "thorough"}, 6),  # the ceiling caps the default
    ({"default": "exhaustive"}, 8),
    ({"default": "nonsense"}, 4),
    (None, 4),
])
def test_fanout_follows_the_agents_effort(tmp_path, effort, expected):
    cfg = {"loop": {"effort": effort}} if effort is not None else {}
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert par.fanout_of(str(tmp_path)) == expected


def test_fanout_without_a_config_is_balanced(tmp_path):
    assert par.fanout_of(str(tmp_path / "missing")) == 4
    assert par.fanout_of(None) == 4


def test_agent_tools_list_marks_every_tool_but_an_approval_gated_one():
    tools = [
        {"name": "web_search", "kind": "external", "description": "Search the web.",
         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "custom_x_write", "kind": "external", "description": "Write.", "approval": True,
         "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}}},
    ]
    client = client_mod.SoleonMcpClient("http://127.0.0.1:1/mcp", token_override="t")
    server = shim.AgentToolsServer("slug", tools, client)
    listed = {t["name"]: t for t in shim.handle_message(server, {"jsonrpc": "2.0", "id": 1,
                                                                  "method": "tools/list"})["result"]["tools"]}
    assert listed["web_search"]["annotations"] == {"readOnlyHint": True}
    assert listed["read_result_page"]["annotations"] == {"readOnlyHint": True}
    gated = [t for n, t in listed.items() if n.startswith("custom_x_write")]
    assert gated and "annotations" not in gated[0]
    assert all("annotations" not in t for t in server.published)  # the published list itself is untouched


def test_workspace_tools_list_marks_all_four(tmp_path):
    ws = ws_mod.Workspace(str(tmp_path))
    listed = ws_mod.handle_message(ws, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in listed] == ["read_file", "write_file", "edit_file", "list_dir"]
    assert all(t["annotations"] == {"readOnlyHint": True} for t in listed)


def test_workspace_server_runs_a_batch_with_the_write_before_its_read(tmp_path):
    """The real process, sent a whole step at once (as Claude Code sends one): every answer comes back, and the
    read sees the write that came before it."""
    root = tmp_path / "workspace"
    root.mkdir()
    proc = subprocess.Popen([sys.executable, str(BIN / "soleon_workspace_mcp.py"), str(root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    batch = [_msg(1, "write_file", path="a.txt", content="hello"), _msg(2, "read_file", path="a.txt"),
             _msg(3, "list_dir", path=".")]
    out, _err = proc.communicate(("\n".join(json.dumps(m) for m in batch) + "\n").encode(), timeout=30)
    by = {r["id"]: r for r in (json.loads(l) for l in out.decode().splitlines() if l.strip())}
    assert set(by) == {1, 2, 3}
    assert "hello" in by[2]["result"]["content"][0]["text"]


def test_agent_tools_serve_answers_calls_out_of_order_without_mixing_lines(tmp_path):
    """serve(): a slow call does not hold back the next one, and each answer is one whole line."""
    class Slow:
        agent_dir = str(tmp_path)

    def fake_handle(server, msg):
        if msg.get("method") == "tools/call":
            time.sleep(0.4 if msg["id"] == 1 else 0.0)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"pad": "x" * 20000}}

    stdin = io.BytesIO(("\n".join(json.dumps(m) for m in [_msg(1), _msg(2)]) + "\n").encode())
    stdout = io.BytesIO()
    real = shim.handle_message
    shim.handle_message = fake_handle
    try:
        shim.serve(Slow(), stdin=stdin, stdout=stdout)
    finally:
        shim.handle_message = real
    lines = [json.loads(l) for l in stdout.getvalue().decode().splitlines()]
    assert [r["id"] for r in lines] == [2, 1]


def _ask_together(tracker, n=8):
    """n calls ask the tracker at the same instant; their answers."""
    start, seen, lock = threading.Barrier(n), [], threading.Lock()

    def ask():
        start.wait()
        got = tracker.current()
        with lock:
            seen.append(got)

    threads = [threading.Thread(target=ask) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return seen


def test_a_steps_first_calls_make_one_conversation_id(tmp_path, monkeypatch, capsys):
    """A step's first calls ask together: ONE conversation id is made and saved, not one per call, and no call's
    save is lost (on Windows two saves at once refuse each other: "Access is denied")."""
    import uuid
    made, real_uuid4 = [0], uuid.uuid4

    def uuid4():
        made[0] += 1
        return real_uuid4()

    monkeypatch.setattr(uuid, "uuid4", uuid4)

    class Slow(shim.ConversationTracker):  # a wide window between reading the file and settling the id
        def _load(self):
            time.sleep(0.05)
            return super()._load()

    seen = _ask_together(Slow(str(tmp_path / ".local-conversation.json")))
    assert len(set(seen)) == 1 and made[0] == 1
    saved = json.loads((tmp_path / ".local-conversation.json").read_text(encoding="utf-8"))
    assert saved["conversation"] == seen[0]
    assert "could not persist" not in capsys.readouterr().err
    assert not list(tmp_path.glob("*.tmp"))


def test_a_save_waits_out_a_brief_refusal(tmp_path, monkeypatch):
    """The other server holding the file for a moment (Windows refuses the replace) does not lose the save."""
    real, refused = os.replace, [0]

    def replace(src, dst):
        if refused[0] < 3:
            refused[0] += 1
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    shim._write_json_atomic(str(tmp_path / "f.json"), {"a": 1})
    assert json.loads((tmp_path / "f.json").read_text(encoding="utf-8")) == {"a": 1} and refused[0] == 3


def test_a_steps_first_calls_make_one_turn_id(tmp_path, monkeypatch):
    """A step's first calls ask together: ONE turn id is made, so every call lands in the same turn."""
    made = [0]

    def mint():
        made[0] += 1
        return "turn{:08d}".format(made[0])

    monkeypatch.setattr(shim, "mint_turn_id", mint)

    class Slow(shim.TurnTracker):
        def _load(self):
            time.sleep(0.05)
            return super()._load()

    seen = _ask_together(Slow(str(tmp_path / ".local-turn.json")))
    assert len(set(seen)) == 1 and made[0] == 1

def test_long_result_pages_get_distinct_ids_when_calls_finish_together():
    client = client_mod.SoleonMcpClient("http://127.0.0.1:1/mcp", token_override="t")
    server = shim.AgentToolsServer("slug", [], client)
    big = {"content": [{"type": "text", "text": "y" * (shim.RESULT_PAGE_CHARS + 10)}]}
    ids, lock = [], threading.Lock()

    def page():
        text = server._paged("t", big)["content"][0]["text"]
        with lock:
            ids.append(text.split("id=")[1].split()[0])

    threads = [threading.Thread(target=page) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 10


def test_two_refused_calls_renew_the_token_once():
    """Both calls of a step are refused with the old token: one renewal, and both retry with it."""
    client = client_mod.SoleonMcpClient("http://127.0.0.1:1/mcp")
    client._token, client._entry = "old", {"refreshToken": "r", "clientId": "c", "_source": "memory"}
    grants, barrier = [0], threading.Barrier(2)

    def post(payload, token):
        if token == "old":
            try:
                barrier.wait(timeout=2)  # both are refused before either renews
            except threading.BrokenBarrierError:
                pass
            raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"content": [{"type": "text", "text": "{}"}]}}

    def grant():
        time.sleep(0.2)
        grants[0] += 1
        client._token = "new{}".format(grants[0])
        return True

    client._post = post
    client._try_refresh_locked = grant
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.call_tool("x", {}))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert grants[0] == 1 and len(results) == 2


# --- PlatformBatcher: a step's platform calls as ONE Soleon request (2026-11-24) ---

def _call(name, **over):
    return dict({"slug": "bot", "app_env": "dev", "name": name, "args": {"q": name}, "approved": False,
                 "conversation": "c1", "turn": "t1"}, **over)


def _done(results):
    return {"status": 200, "state": "done", "call_id": "lc_b", "op": "tool_calls", "session_id": "s",
            "result": {"results": results}}


def _run_together(batcher, calls):
    answers = [None] * len(calls)

    def go(i, c):
        answers[i] = batcher.run(c)
    threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(calls)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    return answers


def test_a_steps_calls_go_as_one_batch_and_each_gets_its_own_answer():
    sent = {"one": [], "batch": []}

    def batch(calls):
        sent["batch"].append([c["name"] for c in calls])
        return _done([{"tool": c["name"], "result": "r-" + c["name"]} if c["name"] != "bad"
                      else {"error": "tool_error", "tool": "bad", "detail": "boom"} for c in calls])
    b = par.PlatformBatcher(lambda c: sent["one"].append(c["name"]), batch, window_s=0.05)
    answers = _run_together(b, [_call("web_search"), _call("bad"), _call("web_fetch")])
    assert sent["one"] == [] and len(sent["batch"]) == 1
    assert sorted(sent["batch"][0]) == ["bad", "web_fetch", "web_search"]
    by_tool = {a.get("result", {}).get("tool") or a.get("tool"): a for a in answers}
    assert by_tool["web_search"] == {"status": 200, "state": "done", "call_id": "lc_b", "op": "tool_call",
                                     "session_id": "s", "result": {"tool": "web_search", "result": "r-web_search"}}
    assert by_tool["bad"]["state"] == "error" and by_tool["bad"]["error"] == "tool_error"
    assert by_tool["bad"]["detail"] == "boom" and by_tool["bad"]["op"] == "tool_call"


def test_a_call_alone_goes_as_before():
    sent = []
    b = par.PlatformBatcher(lambda c: sent.append(c["name"]) or {"state": "done"},
                            lambda calls: pytest.fail("no batch for one call"), window_s=0.01)
    assert b.run(_call("web_search")) == {"state": "done"} and sent == ["web_search"]


def test_an_approved_call_and_another_turns_call_never_join_a_batch():
    sent = {"one": [], "batch": []}
    b = par.PlatformBatcher(lambda c: sent["one"].append(c["name"]) or {"state": "done"},
                            lambda calls: sent["batch"].append([c["name"] for c in calls]) or _done(
                                [{"tool": c["name"]} for c in calls]), window_s=0.05)
    _run_together(b, [_call("a"), _call("b"), _call("ok", approved=True), _call("other", turn="t2")])
    assert sorted(sent["one"]) == ["ok", "other"]
    assert [sorted(x) for x in sent["batch"]] == [["a", "b"]]


def test_an_older_platform_without_batches_falls_back_to_one_by_one_for_good():
    sent = {"one": [], "batch": 0}

    def batch(calls):
        sent["batch"] += 1
        raise par.BatchUnsupported("no call_agent_tools")
    b = par.PlatformBatcher(lambda c: sent["one"].append(c["name"]) or {"state": "done", "n": c["name"]},
                            batch, window_s=0.05)
    answers = _run_together(b, [_call("a"), _call("b")])
    assert sorted(a["n"] for a in answers) == ["a", "b"] and sent["batch"] == 1
    _run_together(b, [_call("c"), _call("d")])
    assert sent["batch"] == 1 and sorted(sent["one"]) == ["a", "b", "c", "d"]


def test_a_failed_batch_fails_every_call_of_it():
    def batch(calls):
        raise client_mod.TransportError("down")
    b = par.PlatformBatcher(lambda c: pytest.fail("not alone"), batch, window_s=0.05)
    errors = []

    def go(c):
        try:
            b.run(c)
        except client_mod.TransportError as exc:
            errors.append(str(exc))
    threads = [threading.Thread(target=go, args=(c,)) for c in (_call("a"), _call("b"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert errors == ["down", "down"]


def test_split_batch_answers_a_whole_batch_failure_to_every_call():
    err = {"status": 200, "state": "error", "error": "internal_error", "detail": "x"}
    assert par.split_batch(err, 3) == [err, err, err]
    bad = par.split_batch(_done([{"tool": "a"}]), 2)
    assert [a["error"] for a in bad] == ["batch_result_malformed"] * 2


# --- the session's broker: the file server's calls join the agent server's batch ---

class _FakeBatcher:
    def __init__(self, answer=None, error=None):
        self.calls, self.answer, self.error = [], answer, error

    def run(self, call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return dict(self.answer or {}, name=call["name"])


def test_the_file_servers_calls_reach_the_agent_servers_batcher(tmp_path):
    host = _FakeBatcher(answer={"state": "done"})
    broker = par.BatchBroker(str(tmp_path), host).start()
    try:
        local = _FakeBatcher(answer={"state": "local"})
        remote = par.RemoteBatcher(str(tmp_path), local)
        assert remote.run(_call("write_file")) == {"state": "done", "name": "write_file"}
        assert [c["name"] for c in host.calls] == ["write_file"] and local.calls == []
    finally:
        broker.close()
    assert not os.path.exists(par.broker_file(str(tmp_path)))


def test_a_broker_error_is_raised_as_the_same_kind_on_the_file_server(tmp_path):
    par.register_errors(client_mod.ToolError, client_mod.TransportError)
    broker = par.BatchBroker(str(tmp_path), _FakeBatcher(error=client_mod.ToolError("refused", {"error": "x"}))).start()
    try:
        with pytest.raises(client_mod.ToolError) as exc:
            par.RemoteBatcher(str(tmp_path), _FakeBatcher()).run(_call("write_file"))
        assert "refused" in str(exc.value) and exc.value.payload == {"error": "x"}
    finally:
        broker.close()


def test_without_a_broker_the_file_server_batches_alone(tmp_path):
    local = _FakeBatcher(answer={"state": "local"})
    assert par.RemoteBatcher(str(tmp_path), local).run(_call("read_file"))["state"] == "local"
    # an announcement left by a server that is gone: the call still runs, here
    with open(par.broker_file(str(tmp_path)), "w") as fh:
        json.dump({"port": 1, "token": "x"}, fh)
    assert par.RemoteBatcher(str(tmp_path), local).run(_call("read_file"))["state"] == "local"


def test_the_broker_serves_only_the_token_holder(tmp_path):
    import socket
    host = _FakeBatcher(answer={"state": "done"})
    broker = par.BatchBroker(str(tmp_path), host).start()
    try:
        with socket.create_connection(("127.0.0.1", broker.port), timeout=2) as conn:
            conn.sendall((json.dumps({"token": "wrong", "call": _call("write_file")}) + "\n").encode())
            out = json.loads(par._read_line(conn))
        assert out["error"]["type"] == "PermissionError" and host.calls == []
    finally:
        broker.close()


# --- review fixes (2026-11-24) ---

def test_two_servers_starting_a_conversation_together_settle_on_one_id(tmp_path):
    """A conversation's first step runs a write and a fetch at once, in the two servers: both must send the
    same conversation id, or one local conversation becomes two platform sessions."""
    path = str(tmp_path / ".local-conversation.json")
    trackers = [shim.ConversationTracker(path) for _ in range(6)]
    ids, barrier = [None] * 6, threading.Barrier(6)

    def go(i):
        barrier.wait()
        ids[i] = trackers[i].current()
    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert len(set(ids)) == 1 and ids[0]
    assert json.load(open(path))["conversation"] == ids[0]
    assert not os.path.exists(path + ".lock")


def test_a_batch_stops_below_the_platforms_size_cap():
    big = "x" * 120_000
    slots = [{"call": _call("write_file", args={"content": big})}, {"call": _call("write_file", args={"content": big})},
             {"call": _call("read_file")}]
    chunks = par.PlatformBatcher._chunks(slots)
    assert [len(c) for c in chunks] == [1, 2]
    many = [{"call": _call("read_file")} for _ in range(20)]
    assert [len(c) for c in par.PlatformBatcher._chunks(many)] == [16, 4]


@pytest.mark.parametrize("text, refused", [
    ("Unknown tool: call_agent_tools", True),
    ('{"error": "op must be one of [...]", "code": "BAD_OP"}', True),
    ("local_control.op must be one of ('tool_list', 'tool_call', 'system_prompt', 'turn_record')", True),
    ("local_control.op must be one of ('tool_list', 'tool_call', 'tool_calls', 'system_prompt')", False),
    ("web_fetch failed: page not found", False),
])
def test_batch_refused_reads_every_older_platform_answer(text, refused):
    assert par.batch_refused(text) is refused


def test_an_older_container_makes_the_calls_go_one_by_one(monkeypatch):
    srv = shim.AgentToolsServer("bot", [], object(), app_env="dev")
    old = {"status": 200, "state": "error", "error": "internal_error",
           "detail": "local_control.op must be one of ('tool_list', 'tool_call', 'system_prompt', 'turn_record')"}
    monkeypatch.setattr(srv, "client", type("C", (), {"call_tool": lambda self, name, body: old})())
    monkeypatch.setattr(srv, "_follow", lambda env, what: env)
    with pytest.raises(par.BatchUnsupported):
        srv._send_batch([_call("a"), _call("b")])


def test_more_than_32_model_answers_go_in_chunks(monkeypatch):
    sent = []

    class C:
        def call_tool(self, name, body):
            sent.append(len(body.get("model_answers") or []))
            return {"state": "done", "result": {}}
        _sleep = staticmethod(lambda s: None)
    reqs = [{"request_id": "r%d" % i} for i in range(70)]
    env = {"state": "pending", "call_id": "lc_x", "model_requests": reqs}
    client_mod.await_control(C(), "bot", env, answer_models=lambda rs: [{"request_id": r["request_id"],
                                                                        "completion": {}} for r in rs])
    assert sent == [32, 32, 6]
