# 子代理事件流 → 可读单行。用法：... 2>&1 | tee raw.jsonl | jq -R -r --unbuffered -f sa-view.jq
def short: (if type == "string" then . else tojson end) | gsub("\\s+"; " ") | .[0:200];

(fromjson? // {raw: .}) |
if .raw then .raw
# agy / droid / grok / pi ACP & RPC (sa-agy / sa-droid / sa-grok / sa-pi / zomi-agy-acp)
elif .event == "init" then "▶ \(.agent // "agy") 会话 \(.conversation_id)（\(.init.model // "acp")）"
elif .event == "step_update" then
  .step_update as $s
  | if $s.step_type != "tool" then empty
    elif $s.state == "ACTIVE" then "→ \($s.tool_name) \($s.tool_info.parameters | short)"
    elif $s.tool_info.output then "  ✓ \($s.tool_info.output | short)"
    else empty end
elif (.event == "quota_fallback" or .type == "quota_fallback") then "⚡ 配额预检：\(.from_agent // "agy")/\(.from_model) 剩余 \(((.rem_frac // 0) * 1000 | round) / 10)%（≤\(((.threshold // 0.05) * 1000 | round) / 10)%），已自动切换至 \(.to_agent // "droid")/\(.to_model)\(if (.handoff_from_session // "") != "" then "（已迁移原会话上下文）" else "" end)"
elif .event == "result" then "■ \(.result.status) \(.result.error // "")\n\(.result.response // "")"
elif .type == "tool_call" and (.item | type == "object") then
  if .item.status == "completed" then (if .item.output then "  ✓ \(.item.output | short)" else empty end)
  elif .item.status == "failed" then "  ✗ \(.item.output // .item.content | short)"
  else "→ \(.item.name // "tool") \(.item.arguments | short)" end
# codex app-server / sa-codex / codex exec --json
elif .type == "thread.started" then "▶ codex 会话 \(.thread_id)"
elif .type == "item.started" and .item.type == "command_execution" then "→ \(.item.command | short)"
elif .type == "item.completed" and .item.type == "command_execution" then "  ✓ exit=\(.item.exit_code) \(.item.aggregated_output | short)"
elif .type == "item.completed" and .item.type == "agent_message" and (.delta | not) then "💬 \(.item.text)"
elif .type == "item.completed" and .item.type == "error" then (if ((.item.message // "") | test("bypass-hook-trust")) then empty else "⚠ \(.item.message | short)" end)
elif .type == "item.completed" and .item.type != "reasoning" and (.delta | not) then "· \(.item.type) \(.item | del(.id, .type) | short)"
elif .type == "turn.completed" then (if .usage then "■ 完成（输入 \(.usage.input_tokens // 0)，输出 \(.usage.output_tokens // 0)）" else "■ 完成" end)
elif .type == "turn.failed" or .type == "error" then "✗ \(. | short)"
# claude --output-format stream-json --verbose
elif .type == "system" and .subtype == "init" then "▶ claude 会话 \(.session_id)（\(.model)）"
elif .type == "assistant" then ([.message.content[]? | if .type == "tool_use" then "→ \(.name) \(.input | short)" else empty end] | if length > 0 then join("\n") else empty end)
elif .type == "user" then ([.message.content[]? | if .type == "tool_result" then "  ✓ \(.content | short)" else empty end] | if length > 0 then join("\n") else empty end)
elif .type == "result" then "■ \(if .is_error then "ERROR" else (.subtype // "done") end)\n\(.result // "")"
# grok --output-format streaming-json
elif .type == "tool_call" then "→ \(.toolName // .title) \(.rawInput | short)"
elif .type == "tool_call_update" and .status == "completed" and .rawOutput then "  ✓ \(.rawOutput | short)"
elif .type == "tool_call_update" and .status == "failed" then "  ✗ \(.rawOutput // .content | short)"
elif .type == "end" then "▶ grok 会话 \(.sessionId)（\((.modelUsage // {} | keys[0]) // "grok-4.7")）\n■ \(if .stopReason == "error" then "ERROR" else (.stopReason // "done") end)（输入 \(.usage.input_tokens // 0)，缓存 \(.usage.cache_read_input_tokens // 0)，输出 \(.usage.output_tokens // 0)）"
else empty end
