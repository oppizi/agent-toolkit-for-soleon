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
#   anyone. Every "permission_mode" value in the input must be a person mode
#   for an "ask", so a value the model writes into tool_input can only make
#   the answer stricter.
#
# Verified on Claude Code 2.1.325: the hook input carries permission_mode,
# then tool_name, then tool_input, on one line; `claude -p` with no mode flag
# (and with --permission-mode manual) sends "default"; a "deny" is honoured
# in bypassPermissions, dontAsk, auto and acceptEdits (the call lands in
# permission_denials and never reaches the server).
#
# The tool name is read only from the text before "tool_input", so arguments
# the model writes cannot choose which tool the question names.
#
# POSIX shell, sed and tr only (no Python): nothing here is slow enough to
# reach the hook's timeout, and without tr no mode is read, so it denies. A
# killed hook is a non-blocking error after which the call would fall to the
# permission rules. It never answers "allow".
#
# Off switch: the plugin option `ask_before_production` set to `off` (user
# settings, through /config), which Claude Code hands to the hook as
# CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION. A project's settings `env` block
# can set that variable too (verified on 2.1.325), so "off" is ignored when the
# project's .claude/settings.json or settings.local.json names the option.
# "off" only removes the question in a person mode: the other modes still
# deny. While it is off, every gated call prints a notice saying so.

input=$(cat)
head=${input%%\"tool_input\"*}
flat=$(printf '%s' "$input" | tr -d '[:space:]' 2>/dev/null)

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

# person=yes only when at least one permission_mode value is present and every
# one of them is a mode where a person sees a question.
person=unknown
mode=
rest=$flat
while :; do
    case "$rest" in
        *'"permission_mode":"'*) ;;
        *) break ;;
    esac
    rest=${rest#*\"permission_mode\":\"}
    m=${rest%%\"*}
    case "$m" in
        default|acceptEdits|plan)
            [ "$person" = unknown ] && person=yes
            [ -n "$mode" ] || mode=$m ;;
        *)
            person=no
            mode=$m
            break ;;
    esac
done
case "$mode" in
    ''|*[!A-Za-z]*) shown="an unreadable" ;;
    *) shown=$mode ;;
esac

off_hint="To turn this off, set the soleon plugin option ask_before_production to off (in /config; it is saved in your user settings)."

off=no
project_note=
if [ "${CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION-}" = "off" ]; then
    off=yes
    dir=${CLAUDE_PROJECT_DIR:-.}
    for f in "$dir/.claude/settings.json" "$dir/.claude/settings.local.json"; do
        [ -f "$f" ] || continue
        case "$(cat "$f" 2>/dev/null)" in
            *ask_before_production*|*ASK_BEFORE_PRODUCTION*)
                off=no
                project_note=" Its off switch is ignored: this project's .claude settings name ask_before_production, and the switch is honoured only from your user settings." ;;
        esac
    done
fi

if [ "$person" = yes ] && [ "$off" = yes ]; then
    printf '%s\n' "{\"systemMessage\":\"Soleon: the ask-before-production hook is OFF (plugin option ask_before_production=off): $what, and it runs without a question.\"}"
    exit 0
fi

if [ "$person" = yes ]; then
    decision=ask
    reason="Soleon: $what, so the soleon plugin asks before running it.$project_note $off_hint"
else
    decision=deny
    reason="Soleon: $what. This session runs in $shown permission mode, where no person sees a question, so the soleon plugin refuses it; the off switch does not change that. Run it in default mode to be asked.$project_note"
fi

printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"$decision\",\"permissionDecisionReason\":\"$reason\"}}"
exit 0
