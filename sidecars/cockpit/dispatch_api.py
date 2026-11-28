"""Loopback dispatch settings routes. This module never dispatches work."""
from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

import dispatch_policy

_CSRF_TOKEN = secrets.token_urlsafe(24)
_STATIC_DIR = Path(__file__).resolve().parent
_ASSETS = {
    "/static/dispatch_drawer.catalog.js": ("dispatch_drawer.catalog.js", "text/javascript; charset=utf-8"),
    "/static/dispatch_drawer.js": ("dispatch_drawer.js", "text/javascript; charset=utf-8"),
    "/static/dispatch_drawer.css": ("dispatch_drawer.css", "text/css; charset=utf-8"),
}
_CAPABILITIES = "/api/dispatch-capabilities"
_POLICY = "/api/dispatch-policy"
_RESET = f"{_POLICY}/reset"


class WriteRequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def get_csrf_token():
    return _CSRF_TOKEN


def _single(headers, key):
    values = headers.get_all(key, [])
    return values[0] if len(values) == 1 else None


_LOOPBACK_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?$")


def verify_write_request(handler, expected_host_port):
    """Validate framing, exact trusted Origin, and CSRF before reading any body."""
    headers = handler.headers
    origins = headers.get_all("Origin", [])
    if len(origins) > 1:
        raise WriteRequestError(403, "invalid origin")
    origin = origins[0] if len(origins) == 1 else None
    plugin_origin = f"plugin://agy-cockpit--cockpit-{expected_host_port}"
    proxied_plugin = not origins and _single(headers, "X-Cockpit-Plugin-Origin") in (
        "plugin://agy-cockpit", plugin_origin,
    )
    exact_hosts = {f"{name}:{expected_host_port}" for name in ("localhost", "127.0.0.1", "[::1]")}
    hosts = headers.get_all("Host", [])
    host = hosts[0] if len(hosts) == 1 else None
    if origin == plugin_origin or proxied_plugin:
        if host is None or not _LOOPBACK_HOST.fullmatch(host):
            raise WriteRequestError(403, "invalid host")
    else:
        if host not in exact_hosts:
            raise WriteRequestError(403, "invalid host")
        if origin not in (f"http://{host}", "plugin://agy-cockpit"):
            raise WriteRequestError(403, "invalid origin")
    token = _single(headers, "X-Cockpit-CSRF")
    if not token or not hmac.compare_digest(token.encode("utf-8"), _CSRF_TOKEN.encode("ascii")):
        raise WriteRequestError(403, "invalid CSRF token")
    if _single(headers, "Content-Type") != "application/json" or "Transfer-Encoding" in headers:
        raise WriteRequestError(400, "expected an application/json body without transfer encoding")
    raw_length = _single(headers, "Content-Length")
    if raw_length is None or not re.fullmatch(r"[0-9]{1,5}", raw_length):
        raise WriteRequestError(400, "invalid content length")
    length = int(raw_length)
    if not 1 <= length <= 65536:
        raise WriteRequestError(400, "invalid content length")
    body = handler.rfile.read(length)
    try:
        if len(body) != length:
            raise ValueError("truncated body")
        payload = dispatch_policy.strict_json_loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload
    except (ValueError, RecursionError) as exc:
        raise WriteRequestError(400, str(exc)) from exc


def handle_dispatch_request(handler, path, qs, state_module):
    respond = handler._respond
    method = handler.command
    if path.startswith("/static/dispatch_drawer."):
        asset = _ASSETS.get(path)
        if asset is None:
            respond(404, {"ok": False, "error": "not_found"})
        elif method not in ("GET", "HEAD"):
            respond(405, {"ok": False, "error": "method_not_allowed"})
        else:
            name, content_type = asset
            try:
                data = (_STATIC_DIR / name).read_bytes()
            except OSError:
                respond(404, {"ok": False, "error": "not_found"})
            else:
                respond(200, data, content_type)
        return

    if path not in (_CAPABILITIES, _POLICY, _RESET):
        respond(404, {"ok": False, "error": "not_found"})
        return
    state_path = Path(state_module.STATE_FILE)
    policy_path = dispatch_policy.policy_file_path(state_path=state_path)
    if method in ("GET", "HEAD") and path in (_CAPABILITIES, _POLICY):
        if path == _CAPABILITIES:
            respond(200, dispatch_policy.probe_capabilities(state_path=state_path))
            return
        current = dispatch_policy.load_policy(policy_path)
        if qs.get("resolve") == ["1"]:
            caps = dispatch_policy.probe_capabilities(state_path=state_path)
            resolved = (
                dispatch_policy.degraded_all(current["read_only_warning"])
                if current["degraded"]
                else dispatch_policy.resolve_all(current["policy"], caps)
            )
            current.update(capabilities=caps, resolved=resolved)
        respond(200, current)
        return
    if method != "POST" or path not in (_POLICY, _RESET):
        respond(405, {"ok": False, "error": "method_not_allowed"})
        return

    # Do not reuse POST connections, especially when a rejected body is unread.
    handler.close_connection = True
    try:
        address = getattr(handler.server, "server_address", None)
        port = (address[1] if address and len(address) > 1 else 0) or handler.server.socket.getsockname()[1]
        payload = verify_write_request(handler, port)
        revision = payload.get("revision")
        if path == _RESET:
            if payload.keys() != {"revision"}:
                raise ValueError("reset accepts only revision")
            updated = dispatch_policy.reset_policy(expected_revision=revision, policy_path=policy_path)
        else:
            updated = dispatch_policy.save_policy(payload, expected_revision=revision, policy_path=policy_path)
    except WriteRequestError as exc:
        respond(exc.status, {"ok": False, "error": str(exc)})
    except dispatch_policy.PolicyConflictError as exc:
        respond(409, {"ok": False, "error": str(exc)})
    except ValueError as exc:
        respond(400, {"ok": False, "error": str(exc)})
    except OSError:
        respond(500, {"ok": False, "error": "cannot save dispatch policy"})
    else:
        respond(200, {"ok": True, "policy": updated, "exists": True,
                      "degraded": False, "read_only_warning": None})
