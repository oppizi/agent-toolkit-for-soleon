#!/bin/sh
# Soleon plugin — ask before every production-tier tool (AHP-1477 spec §9 G7).
#
# hooks.json runs this only for mcp__plugin_soleon_soleon__*_production,
# *_everywhere and call_approved_agent_tool: that matcher is the gate. This
# script does not re-check WHICH tool; it answers every call it is given:
#
# - "ask", with a reason naming the tool and the off switch. In `claude -p`
#   (nobody to answer) an "ask" is refused, even when a permission rule allows
#   the tool (verified on Claude Code 2.1.325).
# - "deny" when the session runs in a permission mode where no person sees a
#   question (bypassPermissions, dontAsk, auto): there an "ask" could be
#   settled without anyone, so the call is refused instead.
#
# POSIX shell, sed and nothing else (no Python): nothing here is slow enough
# to reach the hook's timeout, and a killed hook is a non-blocking error after
# which the call would run unasked. If the tool name cannot be read, the reason
# is generic; the answer is still ask (or deny). It never answers "allow".
#
# Off switch: the plugin option `ask_before_production` set to `off` (user
# settings, through /config). Claude Code hands it to the hook as
# CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION. While it is off, every gated
# call prints a notice saying so, so nobody can turn it off unseen.

input=$(cat)

tool=$(printf '%s' "$input" | sed -n 's/.*"tool_name"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]*\)".*/\1/p' 2>/dev/null | head -n 1)
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

off_hint="To turn this off, set the soleon plugin option ask_before_production to off (in /config; it is saved in your user settings)."

if [ "${CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION-}" = "off" ]; then
    printf '%s\n' "{\"systemMessage\":\"Soleon: the ask-before-production hook is OFF (plugin option ask_before_production=off): $what, and it runs without a question.\"}"
    exit 0
fi

decision=ask
mode=
for m in bypassPermissions dontAsk auto; do
    case "$input" in
        *"\"permission_mode\":\"$m\""*|*"\"permission_mode\": \"$m\""*)
            decision=deny
            mode=$m ;;
    esac
done

if [ "$decision" = deny ]; then
    reason="Soleon: $what. This session runs in $mode mode, where no person sees a question, so the soleon plugin refuses it. Run it in default mode to be asked. $off_hint"
else
    reason="Soleon: $what, so the soleon plugin asks before running it. $off_hint"
fi

printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"$decision\",\"permissionDecisionReason\":\"$reason\"}}"
exit 0
