import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

import dispatch_api
import dispatch_policy
import server


class DispatchAPITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "dispatch-policy.json"
        self.state = self.root / "view_state.json"
        html = self.root / "index.html"
        html.write_bytes(b"<meta content='__CSRF_TOKEN__'><span>__UI_VERSION__</span>")
        patches = (
            patch.object(server, "STATE_FILE", self.state), patch.object(server, "INDEX_HTML_FILE", html),
            patch.object(server, "_HTML_CACHE", None), patch.object(server, "_touch_ui_activity"),
            patch.object(dispatch_api, "_STATIC_DIR", self.root),
            patch.object(dispatch_policy.shutil, "which", side_effect=lambda exe: exe if exe == "sa-codex" else None),
            patch.dict(os.environ, {"SA_DISPATCH_POLICY_PATH": ""}),
        )
        for context in patches:
            context.start()
            self.addCleanup(context.stop)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.StatuslineHandler)
        self.port = srv.server_port
        self.start_server(srv)
        self.headers = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}",
                        "Content-Type": "application/json", "X-Cockpit-CSRF": dispatch_api.get_csrf_token()}

    def start_server(self, srv):
        thread = Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

    def request(self, method, route, body=None, headers=None, extra_headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        try:
            if extra_headers:
                client.putrequest(method, route, skip_host=True)
                for key, value in [*(headers or {}).items(), *extra_headers]:
                    client.putheader(key, value)
                client.endheaders(body)
            else:
                client.request(method, route, body=body, headers=headers or {})
            res = client.getresponse()
            data = res.read()
            mime = res.getheader("Content-Type")
            return res.status, json.loads(data) if data and mime.startswith("application/json") else data, mime
        finally:
            client.close()

    def test_get_resolved_policy_capabilities_and_token_injection(self):
        self.state.write_text(json.dumps({"quota_cache": {"codex-5h": {"pct": 42}}}))
        status, capabilities, _ = self.request("GET", "/api/dispatch-capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(capabilities["available_engines"], ["codex"])
        self.assertEqual(capabilities["quota"]["codex"]["pct"], 42)
        status, raw, _ = self.request("GET", "/api/dispatch-policy")
        self.assertEqual(status, 200)
        self.assertFalse(raw["exists"])
        self.assertNotIn("resolved", raw)
        status, body, _ = self.request("GET", "/api/dispatch-policy?resolve=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["policy"]["revision"], 1)
        self.assertEqual(body["capabilities"], capabilities)
        self.assertEqual(body["resolved"]["deep"]["channel"], "cli")
        status, html, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.headers["X-Cockpit-CSRF"].encode(), html)
        self.assertNotIn(b"__CSRF_TOKEN__", html)

    def test_save_stale_conflict_and_reset(self):
        p = dict(dispatch_policy.default_policy(), enabled=False)
        status, body, _ = self.request("POST", "/api/dispatch-policy", p, self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["policy"], dict(p, revision=2))
        self.assertEqual(json.loads(self.path.read_text()), body["policy"])
        self.assertEqual(self.request("POST", "/api/dispatch-policy", p, self.headers)[0], 409)
        self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": 1}, self.headers)[0], 409)
        self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": 2},
                                      dict(self.headers, Origin="plugin://agy-cockpit"))[1]["policy"],
                         dict(dispatch_policy.default_policy(), revision=3))

    def test_inherited_listener_without_server_port_can_write(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.port = listener.getsockname()[1]
            with patch.dict(os.environ, {"STATUSLINE_LISTEN_FD": str(os.dup(listener.fileno()))}):
                inherited = server._create_server(self.port)
        self.start_server(inherited)
        self.assertFalse(hasattr(inherited, "server_port"))
        host = f"127.0.0.1:{self.port}"
        headers = dict(self.headers, Host=host, Origin=f"http://{host}")
        for revision in (1, 2, 3):
            if revision == 2:
                inherited.server_address = ("127.0.0.1", 0)
            elif revision == 3:
                del inherited.server_address
            self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": revision}, headers)[0], 200)

    def test_csrf_rotation_changes_version_and_renders_new_token(self):
        old_version = server._current_ui_version()
        with patch.object(dispatch_api, "_CSRF_TOKEN", "replacement-token"):
            new_version = server._current_ui_version()
            self.assertNotEqual(new_version, old_version)
            status, html, _ = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(str(new_version).encode(), html)
            self.assertIn(b"replacement-token", html)
            self.assertNotIn(self.headers["X-Cockpit-CSRF"].encode(), html)
            route = "/api/dispatch-policy/reset"
            self.assertEqual(self.request("POST", route, {"revision": 1}, self.headers)[0], 403)
            headers = {**self.headers, "X-Cockpit-CSRF": "replacement-token"}
            self.assertEqual(self.request("POST", route, {"revision": 1}, headers)[0], 200)

    def test_api_uses_policy_environment_override_for_read_save_and_reset(self):
        target = self.root / "override" / "policy.json"
        with patch.dict(os.environ, {"SA_DISPATCH_POLICY_PATH": str(target)}):
            route = "/api/dispatch-policy"
            p = dispatch_policy.default_policy()
            self.assertEqual(self.request("POST", route, p, self.headers)[0], 200)
            self.assertTrue(target.is_file())
            self.assertEqual(self.request("GET", route)[1]["policy"]["revision"], 2)
            self.assertEqual(self.request("POST", route + "/reset", {"revision": 2}, self.headers)[0], 200)
        self.assertFalse(self.path.exists())

    def test_degraded_disk_is_gettable_but_not_writable(self):
        self.path.write_text('{"schema_version": 2}')
        status, body, _ = self.request("GET", "/api/dispatch-policy?resolve=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["degraded"])
        self.assertTrue(body["read_only_warning"])
        for path, payload in (("/api/dispatch-policy", body["policy"]), ("/api/dispatch-policy/reset", {"revision": 1})):
            self.assertEqual(self.request("POST", path, payload, self.headers)[0], 409)
        self.assertEqual(self.path.read_text(), '{"schema_version": 2}')

    def test_rejects_missing_incorrect_csrf_and_nonexact_origins(self):
        for key, value in (
            ("X-Cockpit-CSRF", None), ("X-Cockpit-CSRF", "invalid"), ("X-Cockpit-CSRF", "令牌".encode()),
            ("Origin", None), ("Origin", "null"), ("Origin", "http://localhost"),
            ("Origin", f"http://localhost:{self.port + 1}"), ("Origin", f"http://127.0.0.2:{self.port}"),
            ("Origin", f"http://localhost:{self.port}/"), ("Origin", f"http://user@localhost:{self.port}"),
            ("Origin", f"http://localhost:{self.port}?q=1"), ("Origin", "plugin://other"),
            ("Origin", "plugin://agy-cockpit/"), ("Origin", "http://["),
        ):
            with self.subTest(key=key, value=value):
                headers = dict(self.headers)
                if value is None:
                    del headers[key]
                else:
                    headers[key] = value
                for path in ("/api/dispatch-policy", "/api/dispatch-policy/reset"):
                    self.assertEqual(self.request("POST", path, {"revision": 1}, headers)[0], 403)
        self.assertFalse(self.path.exists())

    def test_allows_exact_ipv4_ipv6_and_localhost(self):
        for revision, name in enumerate(("127.0.0.1", "[::1]", "localhost"), start=1):
            host = f"{name}:{self.port}"
            self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": revision},
                                          dict(self.headers, Host=host, Origin=f"http://{host}"))[0], 200)

    def test_ipv4_host_rejects_ipv6_https_and_mismatched_port_origins(self):
        host = f"127.0.0.1:{self.port}"
        for origin in (f"http://[::1]:{self.port}", f"https://{host}", f"http://127.0.0.1:{self.port + 1}",
                       f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": 1},
                                              dict(self.headers, Host=host, Origin=origin))[0], 403)
        self.assertFalse(self.path.exists())

    def test_rejects_invalid_or_duplicate_host_even_for_webview(self):
        route = "/api/dispatch-policy/reset"
        headers = dict(self.headers, Origin="plugin://agy-cockpit", **{"Content-Length": "14"})
        for host in ("localhost", f"localhost:{self.port + 1}", f"127.0.0.2:{self.port}",
                     f"user@localhost:{self.port}", "[invalid"):
            with self.subTest(host=host):
                self.assertEqual(self.request("POST", route, b'{"revision":1}', dict(headers, Host=host))[0], 403)
        self.assertEqual(self.request("POST", route, b'{"revision":1}', headers,
                                      extra_headers=[("Host", self.headers["Host"])])[0], 403)
        del headers["Host"]
        self.assertEqual(self.request("POST", route, b'{"revision":1}', headers,
                                      extra_headers=[("Accept", "*/*")])[0], 403)
        self.assertFalse(self.path.exists())

    def test_rejects_non_json_duplicate_keys_and_illegal_policy(self):
        original = dispatch_policy.default_policy()
        for body in (dict(original, command="forbidden"), b'{"revision":1,"revision":2}', b'[]', b'\xff',
                     b'{"roles":{"scout":1,"scout":2}}', b'{', b'{"revision": true}'):
            with self.subTest(body=str(body)[:45]):
                self.assertEqual(self.request("POST", "/api/dispatch-policy", body, self.headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/dispatch-policy/reset", {"revision": 1, "unknown": 1}, self.headers)[0], 400)
        self.assertFalse(self.path.exists())

    def test_rejects_invalid_framing_before_read(self):
        for overrides in ({"Content-Type": "application/json; charset=utf-8"}, {"Content-Length": "0"},
                          {"Content-Length": "65537"}, {"Content-Length": "+10"},
                          {"Transfer-Encoding": "chunked"}, {"Transfer-Encoding": ""}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.request("POST", "/api/dispatch-policy", b'{}', {**self.headers, **overrides})[0], 400)
        headers = {**self.headers, "Content-Length": "2"}
        for duplicate, value, status in (("Content-Length", "2", 400), ("Content-Type", "application/json", 400),
                                         ("Origin", self.headers["Origin"], 403),
                                         ("X-Cockpit-CSRF", self.headers["X-Cockpit-CSRF"], 403)):
            self.assertEqual(self.request("POST", "/api/dispatch-policy", b'{}', headers,
                                          extra_headers=[(duplicate, value)])[0], status)

    def test_static_assets_exact_whitelist_and_missing_file(self):
        for suffix, mime in (("js", "text/javascript"), ("css", "text/css")):
            path = f"/static/dispatch_drawer.{suffix}"
            self.assertEqual(self.request("GET", path)[0], 404)
            (self.root / f"dispatch_drawer.{suffix}").write_bytes(b"test asset")
            res = self.request("GET", f"{path}?version=1")
            self.assertEqual(res, (200, b"test asset", mime + "; charset=utf-8"))
            self.assertEqual(self.request("HEAD", path)[1], b"")
            self.assertEqual(self.request("POST", path, headers=self.headers)[0], 405)
        for path in ("/static/dispatch_drawer.txt", "/static/../test_dispatch_api.py",
                     "/static/dispatch_drawer.js/../../index.html", "/static/dispatch_drawer.js%2f..",
                     "/static/dispatch_drawer.js;variant", "/static/%64ispatch_drawer.js",
                     "/api/dispatch-unknown", "/api/dispatch-policy;variant"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_dispatch_drawer_serves_real_assets_and_valid_javascript(self):
        assets = Path(__file__).resolve().parent
        with patch.object(dispatch_api, "_STATIC_DIR", assets):
            for suffix, mime in (("js", "text/javascript"), ("css", "text/css")):
                name = f"dispatch_drawer.{suffix}"
                status, body, content_type = self.request("GET", f"/static/{name}?v=1")
                self.assertEqual(status, 200, name)
                self.assertEqual(content_type, mime + "; charset=utf-8")
                self.assertEqual(body, (assets / name).read_bytes())
        result = subprocess.run(["node", "--check", str(assets / "dispatch_drawer.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
