# 子代理事件流 → 卡片视图；用 jq -nRr --unbuffered --arg raw … -f sa-view.jq。
def report_limit: 8000;
def short: (if type == "string" then . else tojson end) | gsub("\\s+"; " ") | .[0:200];
def tool_name: (. // "tool") | tostring | gsub("[\r\n].*"; "") | if length == 0 then "tool" else .[:40] end;
def model_name: short | .[:48];
def object_or_empty: if type == "object" then . else {} end;
def nonempty_text: if type == "string" and length > 0 then . else null end;
def control($kind; $line): {control: $kind, line: $line};

def decode_event:
  (if $ARGS.named.escaped then fromjson else . end) as $line
  | $line | try (fromjson | if type == "object" then . else $line end) catch $line;

def terminal_result:
  if .event == "result" and (.result | type == "object") then
    .result | ((.status | strings) // "ERROR") as $status
    | control((if $status | startswith("ERROR") then "result_error" else "result" end);
              "■ \($status) \((.error // "") | short)")
      + {body: (.response | nonempty_text)}
  elif .type == "result" then
    control((if .is_error then "result_error" else "result" end);
            "■ \(if .is_error then "ERROR" else (.subtype // "done") end)")
    + {body: (.result | nonempty_text)}
  else null end;

def message_body:
  if .type == "item.completed" and (.delta | not) then
    .item | object_or_empty | if .type == "agent_message" then .text | nonempty_text else null end
  else null end;

# 终态行实时显示；正文只在 EOF 交付，视图和落盘重放共用选择规则。
def records:
  foreach ((inputs | decode_event), null) as $event
    ({latest_body: null};
      .terminal = (if $event | type == "object" then $event | terminal_result else null end)
      | if $event | type == "object" then
          .latest_body = (.terminal.body // ($event | message_body) // .latest_body)
        else . end;
      {event: $event, terminal: .terminal,
       body: (if $event == null then .latest_body else null end)});

def heartbeat($name; $status):
  ($name | tool_name) as $tool
  | if $status == "completed" then "  ✓ \($tool)"
    elif $status == "failed" then "  ✗ \($tool)"
    else "→ \($tool)" end;

def command_completion:
  ((.exit_code // .exitCode | numbers) // null) as $code
  | heartbeat("command"; if ($code != null and $code != 0) or .status == "failed" then "failed" else "completed" end)
    + (if $code == null then "" else " (exit=\($code))" end);

def claude_content: .message | object_or_empty | .content | arrays[] | objects;

def tool_lines:
  (.item | object_or_empty) as $item
  | if .event == "step_update" then
    .step_update | objects | select(.step_type == "tool")
    | if .state == "ACTIVE" then heartbeat(.tool_name; "active")
      elif .state == "FAILED" or .state == "ERROR" then heartbeat(.tool_name; "failed")
      elif .state == "COMPLETED" or (.tool_info | object_or_empty | .output) then heartbeat(.tool_name; "completed")
      else empty end
  elif .type == "tool_call" and (.item | type == "object") then heartbeat($item.name; $item.status)
  elif .type == "item.started" and $item.type == "command_execution" then "→ command"
  elif .type == "item.completed" and (.delta | not) then
    $item | if .type == "command_execution" then command_completion
      elif (.type | IN(null, "reasoning", "agent_message", "user_message", "error")) then empty
      else heartbeat(.type; if .status == "failed" then "failed" else "completed" end) end
  elif .type == "assistant" then claude_content | select(.type == "tool_use") | heartbeat(.name; "active")
  elif .type == "user" then claude_content | select(.type == "tool_result")
    | heartbeat(.name; if .is_error then "failed" else "completed" end)
  elif .type == "tool_call" and .item == null then heartbeat(.toolName // .title; "active")
  elif .type == "tool_call_update" and (.status | IN("completed", "failed")) then
    heartbeat(.toolName // .title; .status)
  else empty end;

def control_rows:
  (.item | object_or_empty) as $item
  | (.usage | object_or_empty) as $usage
  | if .event == "init" then control("session"; "▶ \(.agent // "agy") 会话 \(.conversation_id)（\(((.init | object_or_empty | .model) // "acp") | model_name)）")
  elif (.event == "quota_fallback" or .type == "quota_fallback") then
    (((.rem_frac | numbers) // 0) * 1000 | round / 10) as $remaining
    | (((.threshold | numbers) // 0.05) * 1000 | round / 10) as $threshold
    | control("quota"; "⚡ 配额预检：\(.from_agent // "agy")/\(.from_model) 剩余 \($remaining)%（≤\($threshold)%），已自动切换至 \(.to_agent // "droid")/\(.to_model)\(if (.handoff_from_session // "") != "" then "（已迁移原会话上下文）" else "" end)")
  elif .type == "thread.started" then control("session"; "▶ codex 会话 \(.thread_id)")
  elif .type == "item.completed" and $item.type == "error" then
    (($item.message | strings) // "") | select(test("bypass-hook-trust") | not) | control("warning"; "⚠ \(. | short)")
  elif .type == "turn.completed" then
    control("turn_done"; if (.usage | type == "object") then "■ 完成（输入 \($usage.input_tokens // 0)，输出 \($usage.output_tokens // 0)）" else "■ 完成" end)
  elif .type == "turn.failed" or .type == "error" then control("turn_error"; "✗ \(. | short)")
  elif .type == "system" and .subtype == "init" then control("session"; "▶ claude 会话 \(.session_id)（\(.model | model_name)）")
  elif .type == "end" then
    control("session"; "▶ grok 会话 \(.sessionId)（\(((.modelUsage | object_or_empty | keys[0]) // "grok-4.7") | model_name)）"),
    control((if .stopReason == "error" then "result_error" else "result" end);
            "■ \(if .stopReason == "error" then "ERROR" else (.stopReason // "done") end)（输入 \($usage.input_tokens // 0)，缓存 \($usage.cache_read_input_tokens // 0)，输出 \($usage.output_tokens // 0)）")
  else empty end;

def view_rows:
  records
  | if .terminal != null then .terminal | del(.body)
    elif (.event | type) == "object" then .event | control_rows, (tool_lines | {tool: .})
    elif (.event | type) == "string" then {misc: "! \(.event[:180])"}
    elif .body != null then {body: .body}
    else empty end;

# jq 按字符切片；用字节长度二分边界，避免切碎中文等多字节字符。
def utf8_head($limit):
  .[:$limit] as $text
  | def fit($lo; $hi):
      if $lo == $hi then $text[:$lo]
      else (($lo + $hi + 1) / 2 | floor) as $mid
      | if ($text[:$mid] | utf8bytelength) <= $limit then fit($mid; $hi)
        else fit($lo; $mid - 1) end end;
    if ($text | utf8bytelength) <= $limit then $text else fit(0; $text | length) end;
def reverse_text: explode | reverse | implode;

def preview($bytes):
  utf8_head(2500) as $head
  | (.[-1500:] | reverse_text | utf8_head(1500) | reverse_text) as $tail
  | "（正文共 \($bytes) 字节，已保留头尾）\n\($head)\n…（已省略中间内容）…\n\($tail)";

def line_size: utf8bytelength + 1;

# 视图总预算扣除足额正文和换行。各协议标记独立留额，噪声不能挤掉会话或终态。
def control_budget:
  {session: 165, result: 130, result_error: 215, quota: 158, warning: 42, turn_error: 44, turn_done: 44};
def progress_budget: 1498 - (control_budget | add);

def show_control($row):
  $row.control as $kind | .control[$kind] as $left
  | if ($row.line | line_size) > $left and $left < 16 then .
    elif ($kind | IN("warning", "turn_error", "turn_done")) and .last_control[$kind] == $row.line then .
    else .output = ($row.line | utf8_head($left - 1))
      | .last_control[$kind] = $row.line
      | .control[$kind] -= (.output | line_size)
      | .last = null
    end;

def view($raw):
  "…（后续工具心跳已折叠，完整过程见 \($raw)）" as $path_notice
  | (if ($path_notice | line_size) <= progress_budget then $path_notice else "…（后续工具心跳已折叠，完整过程见原始日志）" end) as $fold_notice
  | foreach view_rows as $row
    ({left: (progress_budget - ($fold_notice | line_size)),
      control: control_budget, last_control: {}, count: 0, last: null, folded: false};
      .output = null
      | if $row.body != null then
          ($row.body | utf8bytelength) as $bytes
          | .output = ($row.body | if $bytes > report_limit then preview($bytes) else . end)
        elif $row.control != null then show_control($row)
        else
          ($row.tool != null) as $is_tool
          | ($row.tool // $row.misc) as $line
          | ($line | line_size) as $size
          | if $is_tool and (.folded or .last == $line) then .
            elif ($is_tool and .count >= 60) or $size > .left then
              if $is_tool then .folded = true | .output = $fold_notice else . end
            else
              .left -= $size | .output = $line
              | if $is_tool then .count += 1 | .last = $line else .last = null end
            end
        end;
      .output // empty);

if $ARGS.named.mode == "report" then
  records | .body | strings | select(utf8bytelength > report_limit)
else view($ARGS.named.raw) end
