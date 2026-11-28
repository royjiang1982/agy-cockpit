"""Regression tests for Orchestrator recognition and Models Board rendering."""
from __future__ import annotations

from contextlib import closing
from html.parser import HTMLParser
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyzer
import models_board
import pricing
import subagents


class _Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.scripts = []
        self._inline_script = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.tags.append((tag, attrs))
        if tag == "script":
            self._inline_script = "src" not in attrs
            if self._inline_script:
                self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self._inline_script = False

    def handle_data(self, data):
        if self._inline_script:
            self.scripts[-1] += data
        elif data.strip():
            self.text.append(data.strip())


# Exercise the shipped inline scripts with only the DOM and transport surfaces
# these scenarios need. No polling loop, network, browser packages, or CSS source.
_UI_TEST_DRIVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {scripts, ids} = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = Object.fromEntries(ids.map(id => {
  const classes = new Set();
  return [id, {
    innerHTML: '', style: {}, handlers: {}, attrs: {},
    classList: {
      contains: name => classes.has(name),
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
    },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    setAttribute(name, value) { this.attrs[name] = value; },
  }];
}));
const calls = [];
const context = vm.createContext({
  window: {location: {search: ''}, addEventListener() {}},
  document: {hidden: true, getElementById: id => nodes[id] || null, addEventListener() {}},
  setInterval() {}, setTimeout() {}, clearTimeout() {}, URLSearchParams, AbortController,
  fetch: async (url, options) => {
    calls.push([url, options.method]);
    return {ok: true, text: async () => '{}'};
  },
});
const run = code => vm.runInContext(code, context, {timeout: 1000, filename: 'index.html'});
scripts.forEach(run);
run(`renderModelPanel([{model: 'Idle'}, {model: 'Neon', prompt: 100}])`);
const board = nodes.modelPanel.innerHTML;
run('renderModelPanel([])');
run(`
  globalThis.fixtureQuota = {
    'gemini-weekly': {pct: 42, estimate_ok: false, estimate_total_usd: 9000},
    'gemini-5h': {pct: 'invalid'},
    '3p-monthly': {pct: 0, estimate_ok: true, estimate_total_usd: 20},
  };
  renderQuotaFootBar(fixtureQuota);
`);
const quota = nodes.qdBody.innerHTML;
run("fixtureQuota['gemini-weekly'].stale = true; renderQuotaFootBar(fixtureQuota)");
const beforeClick = calls.length;
nodes.footBar.handlers.click();
console.log(JSON.stringify({
  board, clearedBoard: nodes.modelPanel.innerHTML, quota, staleQuota: nodes.qdBody.innerHTML,
  geminiTitle: nodes.fbAgyG.title, claudeTitle: nodes.fbAgyC.title,
  beforeClick, calls, expanded: nodes.footBar.attrs['aria-expanded'],
}));
"""


def _varint(val: int) -> bytes:
    out = bytearray()
    v = int(val)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(fnum: int, val: int) -> bytes:
    return _varint((fnum << 3) | 0) + _varint(val)


def _field_bytes(fnum: int, payload: bytes) -> bytes:
    return _varint((fnum << 3) | 2) + _varint(len(payload)) + payload


def _encode_gen_metadata(
    step_idx: int,
    model_enum: int,
    inp: int,
    cache: int,
    out: int,
    max_ctx: int | None = None,
    *,
    bytes_step: bool = False,
) -> bytes:
    usage = _field_varint(2, inp) + _field_varint(3, out) + _field_varint(5, cache)
    f1 = _field_bytes(4, usage) + _field_varint(3, model_enum)
    if max_ctx is not None:
        cwm = _field_varint(1, inp + cache) + _field_varint(4, max_ctx)
        f1 += _field_bytes(9, _field_bytes(10, cwm))
    step_field = _field_bytes(2, _varint(step_idx)) if bytes_step else _field_varint(2, step_idx)
    return _field_bytes(1, f1) + step_field


def _encode_user_step(model_enum: int) -> bytes:
    f1 = _field_varint(1, model_enum)
    return _field_bytes(19, _field_bytes(12, _field_bytes(1, f1)))


def _build_db(path: Path, gen_rows: list[bytes], step_rows: list[tuple[int, int, int, bytes, bytes]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    con.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)"
    )
    for i, blob in enumerate(gen_rows):
        con.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (i, blob))
    for idx, stype, status, meta, payload in step_rows:
        con.execute(
            "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
            (idx, stype, status, meta, payload),
        )
    con.commit()
    con.close()


class OrchestratorRecognitionTests(unittest.TestCase):
    def setUp(self):
        with pricing._DYNAMIC_MODEL_LOCK:
            self._saved_obs = dict(pricing._OBSERVED_MODEL_CTX_LIMITS)
            pricing._OBSERVED_MODEL_CTX_LIMITS.clear()

    def tearDown(self):
        with pricing._DYNAMIC_MODEL_LOCK:
            pricing._OBSERVED_MODEL_CTX_LIMITS.clear()
            pricing._OBSERVED_MODEL_CTX_LIMITS.update(self._saved_obs)

    def test_canonical_label_and_quota_family_for_all_orchestrators(self):
        expected_labels = {
            "gemini-pro-orchestrator": "Carbon",
            "Orchestrator (Carbon)": "Carbon",
            "gemini-pro-orchestrator-argon": "Argon",
            "Orchestrator (Argon)": "Argon",
            "gemini-pro-orchestrator-neon": "Neon",
            "Orchestrator (Neon)": "Neon",
        }
        for raw, label in expected_labels.items():
            self.assertEqual(pricing.canonical_model_label(raw), label, raw)
            self.assertEqual(pricing.model_quota_family(raw), "gemini", raw)

        self.assertEqual(pricing.model_quota_family("Custom Orchestrator"), "gemini")
        self.assertEqual(pricing.model_quota_family("Claude Orchestrator"), "claude")

    def test_resolve_builtin_enum_from_parent_pro_capabilities(self):
        for enum_id in (1667, 1681, 1537, 1532, 1525, 1427, 1417):
            self.assertEqual(analyzer.resolve_builtin_enum_from_parent(enum_id, "pro"), enum_id)
        for enum_id in (1322, 1402, 342):
            self.assertEqual(analyzer.resolve_builtin_enum_from_parent(enum_id, "pro"), 1437)

    def test_length_delimited_gen_metadata_step_index(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-orch-") as tmp:
            db = Path(tmp) / "trajectory.db"
            _build_db(db, [_encode_gen_metadata(130, 1532, 20, 10, 5, bytes_step=True)], [])
            with closing(sqlite3.connect(db)) as con:
                *_, calls = analyzer._extract_sqlite_calls(con.cursor())
            self.assertEqual([(call["step_idx"], call["prompt"]) for call in calls], [(130, 30)])

    def test_continuous_orchestrator_turns_keep_context_limit_and_real_switch_resets(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-orch-ctx-") as tmp:
            db_same = Path(tmp) / "orch-same.db"
            _build_db(
                db_same,
                [_encode_gen_metadata(1, 1532, 20_000, 180_000, 600, max_ctx=1_000_000)],
                [
                    (0, 14, 3, b"", _encode_user_step(1537)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1537)),
                ],
            )
            res_same = analyzer._analyze_conversation_base(
                str(db_same), "orch-same", "t", "app", db_same.stat().st_mtime, include_subagents=False
            )
            self.assertEqual(res_same["context"]["limit"], 1_000_000)

            db_switch = Path(tmp) / "orch-switch.db"
            _build_db(
                db_switch,
                [_encode_gen_metadata(1, 342, 10_000, 50_000, 300, max_ctx=131_072)],
                [
                    (0, 14, 3, b"", _encode_user_step(342)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1437)),
                ],
            )
            res_switch = analyzer._analyze_conversation_base(
                str(db_switch), "orch-switch", "t", "app", db_switch.stat().st_mtime, include_subagents=False
            )
            self.assertEqual(res_switch["context"]["limit"], pricing.CTX_WINDOW_MAX)

            db_orch_switch = Path(tmp) / "orch-switch-tier.db"
            _build_db(
                db_orch_switch,
                [_encode_gen_metadata(1, 1532, 20_000, 180_000, 600, max_ctx=1_000_000)],
                [
                    (0, 14, 3, b"", _encode_user_step(1537)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1667)),
                ],
            )
            res_orch_switch = analyzer._analyze_conversation_base(
                str(db_orch_switch),
                "orch-switch-tier",
                "t",
                "app",
                db_orch_switch.stat().st_mtime,
                include_subagents=False,
            )
            self.assertEqual(res_orch_switch["context"]["limit"], pricing.CTX_WINDOW_MAX)

    def test_builtin_override_is_fallback_only_when_db_call_lacks_model_enum(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-sub-ovr-") as tmp:
            db_real = Path(tmp) / "sub-real.db"
            _build_db(
                db_real,
                [_encode_gen_metadata(1, 1532, 10_000, 20_000, 500, max_ctx=512_000)],
                [(0, 14, 3, b"", b""), (1, 15, 3, b"", b"")],
            )
            res_real = analyzer._analyze_conversation_base(
                str(db_real),
                "sub-real",
                "t",
                "app",
                db_real.stat().st_mtime,
                include_subagents=False,
                model_enum_override=1437,
            )
            self.assertEqual(res_real["model"]["short"], "Neon High")
            self.assertEqual([m["model"] for m in res_real["main_models"]], ["Neon"])

            with analyzer._BUILTIN_ENUM_LOCK:
                prev_sub = analyzer._BUILTIN_SUBAGENT_MODEL_ENUM.get("sub-real")
                analyzer._BUILTIN_SUBAGENT_MODEL_ENUM["sub-real"] = 1437
            try:
                ev_real = analyzer.extract_db_cost_events(db_real, db_real.stat().st_mtime)
            finally:
                with analyzer._BUILTIN_ENUM_LOCK:
                    if prev_sub is None:
                        analyzer._BUILTIN_SUBAGENT_MODEL_ENUM.pop("sub-real", None)
                    else:
                        analyzer._BUILTIN_SUBAGENT_MODEL_ENUM["sub-real"] = prev_sub

            expected_neon_cost = pricing.calc_call_cost("Gemini 4 Neon (High)", 10_000, 20_000, 500)
            self.assertAlmostEqual(ev_real[0][1], expected_neon_cost, places=6)

            db_zero = Path(tmp) / "sub-zero.db"
            _build_db(
                db_zero,
                [_encode_gen_metadata(1, 0, 10_000, 20_000, 500)],
                [(0, 14, 3, b"", b""), (1, 15, 3, b"", b"")],
            )
            res_zero = analyzer._analyze_conversation_base(
                str(db_zero),
                "sub-zero",
                "t",
                "app",
                db_zero.stat().st_mtime,
                include_subagents=False,
                model_enum_override=1532,
            )
            self.assertEqual(res_zero["model"]["short"], "Neon High")
            self.assertEqual([m["model"] for m in res_zero["main_models"]], ["Neon"])

    def test_transcript_fallback_emits_per_model_for_models_board(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-tr-pm-") as tmp:
            tr = Path(tmp) / "transcript.jsonl"
            lines = [
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-pro-orchestrator-neon",
                    "step_index": 1,
                    "input_tokens": 2000,
                    "cache_read_tokens": 8000,
                    "output_tokens": 400,
                    "created_at": "2026-11-25T10:00:00Z",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-3.8-flash-high",
                    "step_index": 2,
                    "input_tokens": 1000,
                    "cache_read_tokens": 4000,
                    "output_tokens": 200,
                    "created_at": "2026-11-25T10:00:05Z",
                    "status": "DONE",
                },
            ]
            tr.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            fb = subagents.analyze_agy_transcript_fallback(tr)
            self.assertIsNotNone(fb)
            self.assertEqual([m["model"] for m in fb["per_model"]], ["Orchestrator (Neon)", "Flash High"])

            board = models_board.build_models_board([], [fb])
            self.assertEqual([it["model"] for it in board["items"]], ["Neon", "Flash"])

    def test_index_html_renders_model_bars_and_refreshes_on_quota_open(self):
        markup = _Markup((ROOT / "index.html").read_text(encoding="utf-8"))
        ids = {"modelPanel", "qdBody", "footBar", "quotaDrawer", "fbAgyG", "fbAgyC"}
        self.assertLessEqual(ids, {attrs.get("id") for _, attrs in markup.tags})
        self.assertTrue(markup.scripts)
        fixture = dict(ids=sorted(ids), scripts=[s.replace("__UI_VERSION__", "1") for s in markup.scripts])
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["node", "-e", _UI_TEST_DRIVER], cwd=tmp, env=dict(os.environ, HOME=tmp, NODE_OPTIONS=""),
                input=json.dumps(fixture), capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        board = _Markup(result["board"])
        self.assertIn("group", [a.get("role") for _, a in board.tags])
        labels = [a["aria-label"] for tag, a in board.tags if tag == "button"]
        self.assertEqual([label.partition(" · ")[0] for label in labels], ["Neon", "Idle"])
        heights = [int(a["style"].removeprefix("height:").removesuffix("px"))
                   for _, a in board.tags if a.get("style", "").startswith("height:")]
        self.assertEqual(len(heights), 2)
        self.assertGreater(heights[0], heights[1])
        self.assertEqual(result["clearedBoard"], "")

        quota = _Markup(result["quota"]).text
        self.assertEqual(_Markup(result["staleQuota"]).text, quota)
        self.assertIn("42%", quota)
        self.assertIn("0%", quota)
        self.assertIn("--", quota)
        self.assertIn("~$20.0", quota)
        self.assertNotIn("~$9,000", quota)
        self.assertIn("42%", result["geminiTitle"])
        self.assertIn("0%", result["claudeTitle"])
        self.assertEqual(result["beforeClick"], 0)
        self.assertEqual(result["calls"], [["/api/quota/refresh?mode=open", "POST"]])
        self.assertEqual(result["expanded"], "true")


if __name__ == "__main__":
    unittest.main()
