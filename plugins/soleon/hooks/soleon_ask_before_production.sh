#!/bin/sh
# Soleon plugin — ask before every production-tier tool (AHP-1477 spec §9 G7).
#
# hooks.json runs this only for mcp__plugin_soleon_soleon__*_production,
# *_everywhere and call_approved_agent_tool. It answers the PreToolUse
# decision "ask" with a reason naming the tool and the off switch. In
# `claude -p` (nobody to answer) an "ask" is refused, even when a permission
# rule allows the tool (verified on Claude Code 2.1.325).
#
# Fail closed: the script re-checks the tool name itself, and when it cannot
# read the call (no Python, unreadable input) it still asks. It never answers
# "allow"; a tool outside the production set gets no answer at all.
#
# Off switch: SOLEON_ASK_BEFORE_PRODUCTION=off (that exact value only).

if [ "${SOLEON_ASK_BEFORE_PRODUCTION-}" = "off" ]; then
    cat >/dev/null
    exit 0
fi

input=$(cat)

generic='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Soleon: this tool changes staging, production or every environment, so the soleon plugin asks first. To turn this off, set SOLEON_ASK_BEFORE_PRODUCTION=off (for example in the env block of your Claude Code settings)."}}'

p=python3
"$p" -c "" 2>/dev/null || p=python
if ! "$p" -c "" 2>/dev/null; then
    printf '%s\n' "$generic"
    exit 0
fi

out=$(printf '%s' "$input" | "$p" -c '
import json, re, sys

GATED = re.compile(r"mcp__plugin_soleon_soleon__(.*_(production|everywhere)|call_approved_agent_tool)")
OFF = "To turn this off, set SOLEON_ASK_BEFORE_PRODUCTION=off (for example in the env block of your Claude Code settings)."

def ask(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "ask",
                                             "permissionDecisionReason": reason}}))

try:
    call = json.loads(sys.stdin.read())
    tool = call.get("tool_name") if isinstance(call, dict) else None
except ValueError:
    tool = None
if not isinstance(tool, str):
    ask("Soleon: the soleon plugin could not read this tool call, so it asks first. " + OFF)
elif GATED.fullmatch(tool):
    if tool.endswith("call_approved_agent_tool"):
        what = "runs an agent tool marked \"requires approval\" as already approved"
    elif tool.endswith("_everywhere"):
        what = "changes something every environment uses at once, production included"
    else:
        what = "changes staging or production"
    ask("Soleon: {} {}, so the soleon plugin asks before running it. {}".format(tool, what, OFF))
')
status=$?
if [ "$status" -ne 0 ]; then
    printf '%s\n' "$generic"
    exit 0
fi
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
