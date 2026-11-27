"""Keep native RPC questions alive and route each answer to its original request."""
import copy
import threading
import time
import uuid

ACP_QUESTION_ENGINES = ("agy", "droid", "grok")


class NativeQuestions:
    def __init__(self, engine, write, emit):
        self.engine, self.write, self.emit = engine, write, emit
        self.lock = threading.RLock()
        self.records = {}
        self.closed = False

    @property
    def _is_acp(self):
        return self.engine in ACP_QUESTION_ENGINES

    def add(self, request_id, arguments):
        with self.lock:
            if self.closed:
                return
            if any(r["request_id"] == request_id for r in self.records.values()):
                return
            token = uuid.uuid4().hex
            self.records[token] = dict(request_id=request_id, state="pending", callback=None)
        self.emit({"type": "native_question", "token": token, "arguments": arguments})

    def bind(self, token, card, callback):
        with self.lock:
            record = self.records[token]
            record.update(card=copy.deepcopy(card), callback=callback)
            state = record["state"]
        if state != "pending":
            callback(state)

    def waiting(self):
        with self.lock:
            return any(r["state"] in ("pending", "sending") for r in self.records.values())

    def resolve(self, token, payload):
        with self.lock:
            record = self.records.get(token)
            if not record or self.closed or record["state"] == "expired":
                raise ValueError("原生提问已失效，未发送回答")
            if record["state"] != "pending":
                raise ValueError("回答已发送或结果未确认，请勿重复提交")
            result = self._answer(record["card"], payload)
            record["answers"] = []
            if not self._is_acp:
                for question in record["card"]["questions"]:
                    values = result["answers"].get(question["id"], {}).get("answers", [])
                    record["answers"].append(
                        dict(
                            id=question["id"],
                            prompt=question["prompt"],
                            display="、".join(values),
                            skipped=not values,
                        )
                    )
            elif result["outcome"]["outcome"] == "selected":
                question = record["card"]["questions"][0]
                option = next(
                    o for o in question["options"] if o["value"] == result["outcome"]["optionId"]
                )
                record["answers"].append(
                    dict(
                        id=question["id"],
                        prompt=question["prompt"],
                        keys=[option["key"]],
                        display=option["label"],
                    )
                )
            record["state"] = "sending"
            request_id = record["request_id"]
        try:
            self.write({"id": request_id, "result": result})
        except Exception:
            self._set(token, "unknown")
            raise ValueError("回答发送结果未确认，未自动重发") from None
        dismissed = payload.get("action") == "dismiss" or (
            self._is_acp and result["outcome"]["outcome"] == "cancelled"
        )
        status = "dismissed" if dismissed else "answered"
        self._set(token, status)
        return status

    def _answer(self, card, payload):
        action = payload.get("action", "select")
        if action not in ("answer", "select", "dismiss"):
            raise ValueError("不支持的提问操作")
        if action == "dismiss":
            return {"outcome": {"outcome": "cancelled"}} if self._is_acp else {"answers": {}}
        questions = card["questions"]
        raw = payload.get("answers")
        if not raw:
            raw = [{"id": questions[0]["id"], "keys": [payload.get("key") or payload.get("option")]}]
        if not isinstance(raw, list) or len(raw) != len(questions):
            raise ValueError("请回答所有问题")
        by_id = {a.get("id"): a for a in raw if isinstance(a, dict)}
        if len(by_id) != len(raw) or set(by_id) != {q["id"] for q in questions}:
            raise ValueError("回答与问题不匹配")
        answers, selected = {}, []
        for question in questions:
            answer = by_id[question["id"]]
            values = []
            if answer.get("skipped"):
                if self._is_acp:
                    return {"outcome": {"outcome": "cancelled"}}
            elif answer.get("was_custom") or answer.get("wasCustom"):
                text = str(answer.get("display") or "").strip()
                if not question["allow_other"] or not text:
                    raise ValueError("此问题不接受自定义答案或答案为空")
                values = [text]
            else:
                keys = answer.get("keys") or ([answer["key"]] if answer.get("key") else [])
                if not isinstance(keys, list) or not keys or (not question["multi_select"] and len(keys) != 1):
                    raise ValueError("请选择有效选项")
                options = {o["key"]: o for o in question["options"]}
                for key in keys:
                    option = options.get(str(key).upper())
                    if not option:
                        raise ValueError("选项不存在")
                    values.append(option["label"])
                    selected.append(option["value"])
            answers[question["id"]] = {"answers": values}
        if self._is_acp:
            if len(selected) != 1:
                raise ValueError("请选择一个原生选项")
            return {"outcome": {"outcome": "selected", "optionId": selected[0]}}
        return {"answers": answers}

    def _set(self, token, state, expected=None):
        with self.lock:
            record = self.records[token]
            if expected is not None and record["state"] != expected:
                return
            record["state"] = state
            callback = record["callback"]
        if callback:
            callback(state)

    def expire_request(self, request_id):
        with self.lock:
            tokens = [
                t
                for t, r in self.records.items()
                if r["request_id"] == request_id and r["state"] == "pending"
            ]
        for token in tokens:
            self._set(token, "expired", expected="pending")

    def close(self):
        with self.lock:
            self.closed = True
            tokens = [t for t, r in self.records.items() if r["state"] == "pending"]
        for token in tokens:
            self._set(token, "expired", expected="pending")


def publish(bridge, target_id, run_id, proc, event):
    """Questions from chat/work/document workers share the Bot's card endpoint."""
    if event.get("type") != "native_question":
        return False
    token = event["token"]
    card = bridge._normalize_question_card(event["arguments"])
    if not card:
        raise ValueError("原生问题格式无效，未代答")
    card.update(id="native-" + token, native_question=token, dismiss_on_move_on=False)
    message_id = "question-" + token
    with bridge._runs_lock:
        owners = bridge.__dict__.setdefault("_native_question_owners", {})
        owners[token] = (target_id, message_id, proc)

    def update(status):
        def mutate(state):
            for message in state.get("conversations", {}).get(target_id, []):
                if message.get("id") == message_id:
                    message["card"]["status"] = status
                    message["card"]["answers"] = copy.deepcopy(
                        proc.questions.records[token].get("answers", [])
                    )
                    if status in ("unknown", "expired"):
                        message["card"]["subtitle"] = (
                            "回答结果未确认，请勿重复提交"
                            if status == "unknown"
                            else "原生提问已失效"
                        )
            pending = any(
                (m.get("card") or {}).get("status") == "pending"
                for m in state.get("conversations", {}).get(target_id, [])
            )
            for bot in state.get("bots", []):
                if bot.get("id") == target_id:
                    bot["needs_attention"] = pending

        bridge.store.mutate(mutate)
        with bridge._runs_lock:
            bridge._native_question_owners.pop(token, None)

    def append(state):
        messages = state.setdefault("conversations", {}).setdefault(target_id, [])
        if not any(m.get("id") == message_id for m in messages):
            messages.append(
                dict(
                    id=message_id,
                    bot_id=target_id,
                    sender="bot",
                    type="message",
                    text="",
                    streaming=False,
                    created_ts=time.time(),
                    run_id=run_id,
                    card=card,
                )
            )
        for bot in state.get("bots", []):
            if bot.get("id") == target_id:
                bot["needs_attention"] = True

    with proc.questions.lock:
        record = proc.questions.records[token]
        record.update(card=copy.deepcopy(card), callback=update)
        card["status"] = record["state"]
        bridge.store.mutate(append)
        status = record["state"]
    if status != "pending":
        update(status)
    return True


def handle_card(bridge, target_id, msg_id, payload):
    with bridge.store._lock:
        message = next(
            (
                m
                for m in bridge.store._state.get("conversations", {}).get(target_id, [])
                if m.get("id") == msg_id
            ),
            None,
        )
        card = copy.deepcopy((message or {}).get("card") or {})
        if message is None and any(
            (m.get("card") or {}).get("native_question")
            for m in bridge.store._state.get("conversations", {}).get(target_id, [])
        ):
            return {"ok": False, "error": "必须指定实际问题卡片，未回退到其他卡片"}
    token = card.get("native_question")
    if not token:
        return None
    if payload.get("card_id") and payload["card_id"] != card["id"]:
        return {"ok": False, "error": "问题卡片不匹配"}
    if card.get("status") in ("answered", "dismissed"):
        return {"ok": True, "already_done": True}
    with bridge._runs_lock:
        owner = getattr(bridge, "_native_question_owners", {}).get(token)
    if not owner or owner[:2] != (target_id, msg_id):
        return {"ok": False, "error": "原生提问已失效或发送结果未确认，未自动重发"}
    try:
        status = owner[2].questions.resolve(token, payload)
        with bridge.store._lock:
            messages = copy.deepcopy(
                bridge.store._state.get("conversations", {}).get(target_id, [])
            )
        return {
            "ok": True,
            "native": True,
            "status": status,
            "items": messages,
            "messages": messages,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
