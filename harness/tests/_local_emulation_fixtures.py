"""Inline fixtures for the local-emulation tests (pull-agent, the two stdio MCP
servers, the save hook). One flat editor document, one tool list, one assembled
prompt — shaped like the platform answers described in the AHP-889 spec and the
server's `local_control_routes` / `get_edit_draft_route`."""
from __future__ import annotations

import json
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = BET_ROOT / "plugins" / "builder"
BIN = PLUGIN / "bin"
PULL_ASSETS = PLUGIN / "skills" / "pull-agent" / "assets"

SLUG = "demo-agent"
SERVER_URL = "https://mcp-dev.oppizi.com/mcp"

SOUL = "# Demo\n\nYou are Demo, a careful assistant. Never invent data.\n"

DOCUMENT = {
    "name": "Demo Agent",
    "description": "A demo",
    "framework": "maverick",
    "soul": SOUL,
    "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "computeMode": "on-demand",
    "access": "private",
    "guardrails": False,
    "tools": [
        {"id": "sys_filesystem_prompt"},
        {"id": "sys_web_prompt"},
        {"id": "custom_echo-server_read"},
        {"id": "custom_echo-server_write", "approval": True},
    ],
    "skills": [
        {
            "id": "cite-sources", "name": "Cite Sources", "description": "Always cite",
            "content": "# Cite Sources\n\nCite every claim.\n", "enabled": True,
            "files": [
                {"path": "refs/style.md", "enabled": True, "content": "APA.\n"},
                {"path": "assets/logo.png", "enabled": True, "size": 1234, "mime": "image/png"},
            ],
        },
        {"id": "off-skill", "name": "Off", "description": "", "content": "off\n", "enabled": False},
    ],
    # the flat shape of DEPLOYED_CONFIG's schedule (the server's _published_flat_agent port)
    "automations": [{"id": "s1", "name": "Daily", "type": "schedule", "schedule": "0 9 * * *", "prompt": "Summarize",
                     "outputChannel": "slack", "timezone": "UTC", "paused": False, "destinationRaw": ""}],
    "userSchedulesEnabled": False,
    "promptCaching": True,
    "promptCacheTtl": "5m",
    "effort": {"default": "thorough", "ceiling": "exhaustive"},
    "loopDimensions": {"thinking": "auto"},
    "tokenBudget": 500000,
    "evals": {
        "sessionCriteria": [],
        "requestCriteria": [],
        "standardEvals": [
            {"id": "terse", "name": "Terse", "inputs": [{"role": "user", "content": "Capital of France?"}],
             "expectedOutput": "Paris", "evaluationCriteria": "One sentence.", "judgeModel": "claude-haiku-4-5-20251001"},
            {"id": "cites", "name": "Cites", "inputs": [{"role": "user", "content": "Population of Lyon?"}],
             "expectedSkills": ["cite-sources"], "judgeModel": "claude-haiku-4-5-20251001"},
        ],
    },
    "subagents": {
        "schemaVersion": 1,
        "subagents": [
            {"id": "subagent_res1", "enabled": True, "direct": True, "canDelegate": False, "name": "Researcher",
             "whenToUse": "Look things up", "useExamples": ["find a source"], "avoidExamples": ["write copy"],
             "instructions": "Research carefully.", "model": "inherit", "effort": {"default": "swift", "ceiling": "balanced"},
             "toolIds": ["custom_echo-server_read", "sys_web_prompt"], "toolModes": {}, "toolDisabled": {},
             "contextMode": "summary", "threadMode": "fresh", "responseMode": "answer", "outputFields": []},
            {"id": "subagent_wri1", "enabled": True, "direct": True, "canDelegate": False, "name": "Writer",
             "whenToUse": "Draft copy", "useExamples": [], "avoidExamples": [], "instructions": "Write clearly.",
             "model": "us.anthropic.claude-opus-4-6-v1", "effort": None, "toolIds": [], "toolModes": {},
             "toolDisabled": {}, "contextMode": "task", "threadMode": "fresh", "responseMode": "fields",
             "outputFields": [{"id": "field_a", "name": "draft", "description": "the copy", "type": "text", "required": True}]},
            {"id": "subagent_off1", "enabled": False, "direct": True, "canDelegate": False, "name": "Disabled",
             "whenToUse": "never", "useExamples": [], "avoidExamples": [], "instructions": "", "model": "inherit",
             "effort": None, "toolIds": [], "toolModes": {}, "toolDisabled": {}, "contextMode": "summary",
             "threadMode": "fresh", "responseMode": "answer", "outputFields": []},
        ],
        "workflows": [
            {"id": "workflow_mgr1", "enabled": True, "name": "Research team", "mode": "manager",
             "whenToUse": "Big research", "memberIds": ["subagent_res1", "subagent_wri1"],
             "delegationInstructions": "Split by source.", "sequenceInstructions": "", "maxParallel": 2,
             "maxRounds": 3, "terminationCondition": "Every question answered.", "failurePolicy": "partial",
             "watcherId": "", "targetType": "all", "targetId": "", "watchAction": "flag", "maxInterventions": 1},
            {"id": "workflow_peer1", "enabled": True, "name": "Debate", "mode": "peer",
             "whenToUse": "Contested calls", "memberIds": ["subagent_res1", "subagent_wri1"],
             "delegationInstructions": "Argue.", "sequenceInstructions": "", "maxParallel": 2, "maxRounds": 2,
             "terminationCondition": "Agreement.", "failurePolicy": "partial", "watcherId": "",
             "targetType": "all", "targetId": "", "watchAction": "flag", "maxInterventions": 1},
        ],
        "delegation": {"maxDepth": 1},
    },
}

DEPLOYED_CONFIG = {
    "agents": {"defaults": {"model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"}},
    "tools": [{"id": "sys_filesystem_prompt"}],
    "schedules": [{"id": "s1", "name": "Daily", "cron": "0 9 * * *", "prompt": "Summarize", "timezone": "UTC", "paused": False}],
    "guardrails": {"piiEnabled": True},
    "evals": {"standardEvals": [], "scoring": {"frequency": "every_10"}, "budget": {"monthlyUsd": 5}},
    "loop": {"dailyTokenBudget": 2000000},
}

TOOLS = [
    {"name": "read_file", "description": "Read the contents of a file.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
     "approval": False, "kind": "workspace", "subagentPair": False, "serverId": None, "serverName": None, "upstreamTool": None},
    {"name": "write_file", "description": "Write.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
     "approval": False, "kind": "workspace", "subagentPair": False, "serverId": None, "serverName": None, "upstreamTool": None},
    {"name": "web_search", "description": "Search the web.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
     "approval": False, "kind": "external", "subagentPair": False, "serverId": None, "serverName": None, "upstreamTool": None},
    {"name": "custom_echo-server_read", "description": "Read via the echo server.", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
     "approval": False, "kind": "external", "subagentPair": True, "serverId": "echo-server", "serverName": "Echo", "upstreamTool": None},
    {"name": "custom_echo-server_write", "description": "Write via the echo server.", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
     "approval": True, "kind": "external", "subagentPair": True, "serverId": "echo-server", "serverName": "Echo", "upstreamTool": None},
]

PROMPT = (
    "# Agent\n\nFollow the personality and instructions in SOUL.md below.\n\n"
    "## Workspace\nYour workspace is at: /mnt/workspace\n"
    "- Long-term memory: /mnt/workspace/memory/MEMORY.md (write important facts here)\n"
    "- Custom skills: /app/agent-config/skills/{skill-name}/SKILL.md — read these with read_file. "
    "Everything under /app/agent-config is readable; only writing there is refused.\n\n"
    "---\n\n" + SOUL + "\n\n---\n\n## Skills\n- cite-sources: /app/agent-config/skills/cite-sources/SKILL.md\n"
)


def tools_envelope() -> dict:
    return {"status": 200, "state": "done", "call_id": "lc_" + "a" * 24, "op": "tool_list",
            "session_id": "ses_x", "result": {"tools": TOOLS}}


def prompt_envelope() -> dict:
    return {"status": 200, "state": "done", "call_id": "lc_" + "b" * 24, "op": "system_prompt",
            "session_id": "ses_x", "result": {"prompt": PROMPT, "sections": PROMPT.split("\n\n---\n\n"),
                                              "model": DOCUMENT["model"], "toolNames": [t["name"] for t in TOOLS] + ["attach_file"]}}


def draft_envelope(has_draft: bool = True, etag: str = "1726560000000") -> dict:
    body = {"agent": json.loads(json.dumps(DOCUMENT)), "hasDraft": has_draft}
    if has_draft:
        body["draftEtag"] = etag
        body["updatedAt"] = etag
    else:
        body["baselineEtag"] = "7"
    return {"status": 200, "body": body}


def config_envelope() -> dict:
    return {"slug": SLUG, "appEnv": "dev", "soul": "deployed soul\n", "config": json.loads(json.dumps(DEPLOYED_CONFIG)),
            "schedules": DEPLOYED_CONFIG["schedules"]}


def skills_envelope() -> dict:
    return {"slug": SLUG, "appEnv": "dev", "contentMode": "full",
            "skills": [{"id": "cite-sources", "name": "Cite Sources", "enabled": True, "content": "# Cite Sources\n", "files": []}]}


def make_pulled_dir(root: Path, *, has_draft: bool = True, with_zip: bool = True) -> Path:
    """`<root>/.soleon/agents/<SLUG>/` with the raw responses saved as the skill would."""
    agent_dir = root / ".soleon" / "agents" / SLUG
    pull = agent_dir / ".pull"
    pull.mkdir(parents=True, exist_ok=True)
    (pull / "draft.json").write_text(json.dumps(draft_envelope(has_draft)), encoding="utf-8")
    (pull / "config.json").write_text(json.dumps(config_envelope()), encoding="utf-8")
    (pull / "skills.json").write_text(json.dumps(skills_envelope()), encoding="utf-8")
    (pull / "workspace.json").write_text(json.dumps({"status": 200, "body": {"url": "https://x/y.zip", "namespace": "soleon_abc", "prefix": "appenv/dev/demo-agent/soleon_abc/", "files": 2, "bytes": 40}}), encoding="utf-8")
    (agent_dir / "tools.json").write_text(json.dumps(tools_envelope()), encoding="utf-8")
    (agent_dir / "prompt.json").write_text(json.dumps(prompt_envelope()), encoding="utf-8")
    if with_zip:
        import zipfile
        with zipfile.ZipFile(str(pull / "workspace.zip"), "w") as zf:
            zf.writestr("memory/MEMORY.md", "# Memory\n- likes tea\n")
            zf.writestr("notes/todo.txt", "buy milk\n")
            zf.writestr("../escape.txt", "nope\n")
    return agent_dir
