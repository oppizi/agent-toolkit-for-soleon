#!/bin/sh
# Soleon plugin — ask before every production-tier tool (AHP-1477 spec §9 G7).
#
# hooks.json runs this only for mcp__plugin_soleon_soleon__*_production,
# *_everywhere and call_approved_agent_tool: that matcher is the gate. This
# script does not re-check WHICH tool; it answers every call it is given:
#
# - "ask", with a reason naming the tool and the off switch, in a permission
#   mode where a person sees a question (default, acceptEdits, plan). In
#   `claude -p` (mode "default", nobody to answer) an "ask" is refused, even
#   when a permission rule allows the tool.
# - "deny" in every other mode (bypassPermissions, dontAsk, auto, any future
#   one) and when no mode can be read: there an "ask" could be settled without
#   anyone.
#
# Both the mode and the tool name are read only from the text before
# "tool_input" (Claude Code 2.1.325 sends permission_mode, then tool_name, then
# tool_input), so nothing the model writes into the arguments can reach them.
# A real captured input is replayed by harness/tests/test_production_ask_hook.py.
#
# POSIX shell, sed and tr only (no Python): nothing here is slow enough to
# reach the hook's timeout, and without tr no mode is read, so it denies. A
# killed hook is a non-blocking error after which the call would fall to the
# permission rules. It never answers "allow".
#
# Off switch: the plugin option `ask_before_production` set to `off` through
# /config, which saves it in the user's own settings file
# (${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json, under pluginConfigs). The
# hook reads that file itself and honours "off" only from it: the variable
# Claude Code hands the hook (CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION) can
# also come from a project's settings `env` block, so it alone never turns the
# question off. "off" only removes the question in a person mode: the other
# modes still deny. While it is off, every gated call prints a notice.

input=$(cat)
head=${input%%\"tool_input\"*}

tool=$(printf '%s' "$head" | sed -n 's/.*"tool_name"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]*\)".*/\1/p' 2>/dev/null | head -n 1)
case "$tool" in
    mcp__plugin_soleon_soleon__*_everywhere)
        what="$tool changes something every environment uses at once, production included" ;;
    mcp__plugin_soleon_soleon__call_approved_agent_tool)
        what="$tool runs an agent tool marked requires-approval as already approved" ;;
    mcp__plugin_soleon_soleon__*_production)
        what="$tool changes staging or production" ;;
    *)
        what="this tool changes staging, production or every environment" ;;
esac

mode=
flat=$(printf '%s' "$head" | tr -d '[:space:]' 2>/dev/null)
case "$flat" in
    *'"permission_mode":"'*)
        rest=${flat##*\"permission_mode\":\"}
        mode=${rest%%\"*} ;;
esac
case "$mode" in
    default|acceptEdits|plan) person=yes ;;
    *) person=no ;;
esac
case "$mode" in
    ''|*[!A-Za-z]*) shown="an unreadable" ;;
    *) shown=$mode ;;
esac

off_hint="To turn this off, set the soleon plugin option ask_before_production to off (in /config; it is saved in your user settings)."

off=no
note=
user_settings=${CLAUDE_CONFIG_DIR:-${HOME:-/nonexistent}/.claude}/settings.json
# Exactly "off", key and value on one line as /config writes them.
if [ -f "$user_settings" ] && [ -n "$(sed -n '/"ask_before_production"[[:space:]]*:[[:space:]]*"off"/p' "$user_settings" 2>/dev/null)" ]; then
    off=yes
fi
if [ "$off" = no ] && [ "${CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION-}" = "off" ]; then
    note=" Its off switch is ignored: ask_before_production is off in this session but not in your user settings, and the switch is honoured only from there."
fi

if [ "$person" = yes ] && [ "$off" = yes ]; then
    printf '%s\n' "{\"systemMessage\":\"Soleon: the ask-before-production hook is OFF (plugin option ask_before_production=off): $what, and it runs without a question.\"}"
    exit 0
fi

if [ "$person" = yes ]; then
    decision=ask
    reason="Soleon: $what, so the soleon plugin asks before running it.$note $off_hint"
else
    decision=deny
    reason="Soleon: $what. This session runs in $shown permission mode, where no person sees a question, so the soleon plugin refuses it; the off switch does not change that. Run it in default mode to be asked.$note"
fi

printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"$decision\",\"permissionDecisionReason\":\"$reason\"}}"
exit 0
