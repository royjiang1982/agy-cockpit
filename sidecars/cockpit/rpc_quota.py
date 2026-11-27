"""Official CLI quota monitors and live Antigravity theme reader."""

from __future__ import annotations

from metric_rates import grok_remaining

import concurrent.futures
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import signal
import time
from pathlib import Path

from analyzer import HOME, cached_paths, log_lines, serialized
from agy_quota import fetch_agy_quota
from quota_sources import codex_rpc_windows as _extract_codex_rpc_windows, codex_rollout_windows
from droid_ledger import parse_droid_reset_sec, droid_reset_precision, anchor_droid_quota
from subagents import CODEX_SESSIONS_DIR, FACTORY_SESSIONS_DIR, GROK_SESSIONS_DIR, parse_iso_ts

_PROBE_LOCK = threading.RLock()
_ACTIVE_PROBES: dict[int, tuple[subprocess.Popen, tuple[int, ...]]] = {}
_PROBES_PAUSED = False
_MAX_PROBE_OUTPUT = 1024 * 1024
_LAST_DROID_SUCCESS_MTIME = 0.0
_LAST_GROK_SUCCESS_MTIME = 0.0


def pause_probes():
    global _PROBES_PAUSED
    with _PROBE_LOCK:
        _PROBES_PAUSED = True


def resume_probes():
    global _PROBES_PAUSED
    with _PROBE_LOCK:
        _PROBES_PAUSED = False


def active_probe_count():
    with _PROBE_LOCK:
        return len(_ACTIVE_PROBES)


def _start_probe(*args, probe_fds=(), **kwargs):
    kwargs.update(start_new_session=True, close_fds=True)
    with _PROBE_LOCK:
        if _PROBES_PAUSED:
            raise RuntimeError('quota probes paused')
        proc = subprocess.Popen(*args, **kwargs)
        _ACTIVE_PROBES[proc.pid] = (proc, tuple(probe_fds))
        return proc


def _finish_probe(proc):
    with _PROBE_LOCK:
        entry = _ACTIVE_PROBES.pop(proc.pid, None)
    if entry is None:
        return
    # PGID is the PID captured at spawn, even if the group leader already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=1.0)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
    for fd in entry[1]:
        with contextlib.suppress(OSError):
            os.close(fd)


def terminate_active_probes():
    with _PROBE_LOCK:
        active = [entry[0] for entry in _ACTIVE_PROBES.values()]
    for proc in active:
        _finish_probe(proc)


def _probe_output(command, *, timeout, text=True, stderr=subprocess.DEVNULL):
    proc = _start_probe(command, stdout=subprocess.PIPE, stderr=stderr, text=text)
    try:
        output, _ = proc.communicate(timeout=timeout)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command)
        return output
    finally:
        _finish_probe(proc)


_CODEX_QUOTA_CACHE: tuple[tuple[str, float], dict[str, dict]] | None = None
_LAST_CODEX_SCAN_TS: float = 0.0
_DROID_QUOTA_CACHE: dict[str, dict] = {}
_LAST_DROID_FETCH_TS: float = 0.0
_LAST_DROID_SESSIONS_MTIME: float = 0.0
_GROK_QUOTA_CACHE: dict[str, dict] = {}
_LAST_GROK_FETCH_TS: float = 0.0
_LAST_GROK_SESSIONS_MTIME: float = 0.0
_AGY_CONFIG_FILE = HOME / ".gemini" / "config" / "config.json"
_THEME_CFG_MTIME_NS: int = -1
_THEME_CFG_CACHE: dict = {
    "mode": "dark",
    "dark": {"background": "#101010", "foreground": "#cccccc", "primary": "#007acc"},
    "light": {"background": "#F9F9F9", "foreground": "#101010", "primary": "#007acc"},
}


def _find_cli_bin(name: str) -> str | None:
    """Return executable path if CLI `name` is installed on the host, else None."""
    found = shutil.which(name)
    if found:
        return found
    for d in (
        HOME / ".local" / "bin",
        HOME / ".grok" / "bin",
        HOME / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        cand = d / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except Exception:
            continue
    return None


def _store_codex_quota(sig: tuple[str, float], payload: dict[str, dict]) -> dict[str, dict]:
    global _CODEX_QUOTA_CACHE, _LAST_CODEX_SCAN_TS
    copied = dict(payload)
    _CODEX_QUOTA_CACHE = (sig, dict(copied))
    _LAST_CODEX_SCAN_TS = time.time()
    return copied


def _fetch_codex_quota_rpc(codex_bin: str, timeout: float = 8.0) -> dict[str, dict]:
    """Query live Codex rate limits via official `codex app-server --stdio` JSON-RPC."""
    proc: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agy-cockpit-codex-quota-") as td:
            proc = _start_probe(
                [codex_bin, "app-server", "--stdio"],
                cwd=td,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            assert proc.stdin is not None and proc.stdout is not None
            fd = proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            buf = b""
            deadline = time.monotonic() + max(2.0, float(timeout))

            def _send(obj: dict) -> None:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            def _wait_id(target_id: int) -> dict | None:
                nonlocal buf
                while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(msg, dict) and msg.get("id") == target_id:
                            return msg
                    rem = max(0.05, min(0.25, deadline - time.monotonic()))
                    r, _, _ = select.select([fd], [], [], rem)
                    if not r:
                        if proc.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
                return None

            _send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "agy-cockpit", "version": "1.0"}, "capabilities": {"experimentalApi": True}},
            })
            if not _wait_id(1):
                return {}
            _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            _send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            rep = _wait_id(2)
            res = rep.get("result") if isinstance(rep, dict) and isinstance(rep.get("result"), dict) else None
            if not res:
                return {}
            return _extract_codex_rpc_windows(res, time.time())
    except Exception:
        return {}
    finally:
        if proc is not None:
            _finish_probe(proc)


@serialized(lambda *args, **kwargs: "codex-quota")
def fetch_codex_quota(force_refresh: bool = False) -> dict[str, dict]:
    """Query live Codex rate limits via `codex app-server` RPC on refresh, falling back to local sessions."""
    global _CODEX_QUOTA_CACHE, _LAST_CODEX_SCAN_TS
    now = time.time()
    if not force_refresh and _CODEX_QUOTA_CACHE is not None and (now - _LAST_CODEX_SCAN_TS) < 3.0:
        return dict(_CODEX_QUOTA_CACHE[1])
    codex_bin = _find_cli_bin("codex")
    if codex_bin is None:
        return _store_codex_quota(("", 0.0), {})
    out: dict[str, dict] = {"codex-installed": {"installed": True}}
    if force_refresh:
        live = _fetch_codex_quota_rpc(codex_bin, timeout=8.0)
        if live:
            out.update(live)
            return _store_codex_quota(("rpc", time.time()), out)
    if not CODEX_SESSIONS_DIR.exists():
        return _store_codex_quota(("", 0.0), out)
    try:
        rollouts = sorted(cached_paths(CODEX_SESSIONS_DIR, "*.jsonl", recursive=True), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return _store_codex_quota(("", 0.0), out)
    if not rollouts:
        return _store_codex_quota(("", 0.0), out)
    newest = rollouts[0]
    try:
        newest_mt = newest.stat().st_mtime
    except Exception:
        return _store_codex_quota(("", 0.0), out)
    sig = tuple((str(path), path.stat().st_mtime_ns) for path in rollouts[:8])
    if not force_refresh and _CODEX_QUOTA_CACHE is not None and _CODEX_QUOTA_CACHE[0] == sig:
        _LAST_CODEX_SCAN_TS = time.time()
        return dict(_CODEX_QUOTA_CACHE[1])

    candidates = []
    for rollout in rollouts[:8]:
        try:
            for line in reversed(list(log_lines(rollout))):
                if 'rate_limits' not in line:
                    continue
                try:
                    obj = json.loads(line)
                    limits = (obj.get('payload') or {}).get('rate_limits')
                    if isinstance(limits, dict):
                        candidates.append((parse_iso_ts(obj.get('timestamp')) or 0.0, limits))
                        break
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            continue
    if candidates:
        observed, limits = max(candidates, key=lambda item: item[0])
        out.update(codex_rollout_windows(limits, observed))

    return _store_codex_quota(sig, out)


def _parse_droid_reset_sec(tail: str) -> int:
    """Parse `↻ 1 day`, `↻ 3h 20min`, or `↻ 45min` from Droid `/limits` output into seconds."""
    return parse_droid_reset_sec(tail)



@serialized(lambda *args, **kwargs: "droid-quota")
def fetch_droid_quota(seed_cache: dict[str, dict] | None = None, force_refresh: bool = False) -> dict[str, dict]:
    """Fetch Droid (Factory) Standard Usage quota via `/limits` PTY probe if `droid` is installed."""
    global _DROID_QUOTA_CACHE, _LAST_DROID_FETCH_TS, _LAST_DROID_SESSIONS_MTIME, _LAST_DROID_SUCCESS_MTIME
    droid_bin = _find_cli_bin("droid")
    if droid_bin is None:
        return {}

    if not _DROID_QUOTA_CACHE and seed_cache:
        for k in ("droid-5h", "droid-weekly", "droid-monthly"):
            if isinstance(seed_cache.get(k), dict) and seed_cache[k].get("rem_frac") is not None:
                _DROID_QUOTA_CACHE[k] = dict(seed_cache[k])

    probe_cwd_path = Path("/tmp/agy-cockpit-droid-probe")
    probe_cwd_path.mkdir(parents=True, exist_ok=True)
    probe_cwd = str(probe_cwd_path)
    probe_slug_dir = FACTORY_SESSIONS_DIR / "-tmp-agy-cockpit-droid-probe"

    max_s_mt = 0.0
    if FACTORY_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(FACTORY_SESSIONS_DIR, "*.settings.json", recursive=True):
                try:
                    jl = p.with_name(p.name[: -len(".settings.json")] + ".jsonl")
                    if jl.exists() and jl.stat().st_size < 450:
                        continue
                    max_s_mt = max(max_s_mt, p.stat().st_mtime)
                except Exception:
                    continue
        except Exception:
            pass

    now = time.time()
    _LAST_DROID_SESSIONS_MTIME = max(_LAST_DROID_SESSIONS_MTIME, max_s_mt)
    if not force_refresh:
        return {"droid-installed": {"installed": True}, **_DROID_QUOTA_CACHE}

    _LAST_DROID_FETCH_TS = now
    _LAST_DROID_SESSIONS_MTIME = max_s_mt
    pre_probe_files = set(probe_slug_dir.iterdir()) if probe_slug_dir.exists() else set()
    master_fd = -1
    slave_fd = -1
    proc = None
    buf = b""
    try:
        master_fd, slave_fd = pty.openpty()
        with contextlib.suppress(Exception):
            os.set_inheritable(master_fd, False)
            os.set_inheritable(slave_fd, False)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        proc = _start_probe(
            [droid_bin], probe_fds=(master_fd,),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=probe_cwd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        sent = 0
        trusted = False
        enter_offset = 0
        deadline = time.monotonic() + 8.0
        t_sent = time.monotonic()
        enter_offset = 0
        while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
            r, _, _ = select.select([master_fd], [], [], 0.15)
            if r:
                try:
                    chunk = os.read(master_fd, 16384)
                    if not chunk:
                        break
                    buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
                except OSError:
                    break
            if not trusted and b"agy-cockpit-droid-probe" in buf and (b"\xe4\xbf\xa1\xe4\xbb\xbb\xe6\xad\xa4\xe6\x96\x87\xe4\xbb\xb6\xe5\xa4\xb9" in buf or b"Trust this" in buf):
                time.sleep(0.2)
                os.write(master_fd, b"\r")
                trusted = True
                buf = b""
                continue
            if sent == 0 and (b"Model" in buf or b"\xe8\xaf\x95\xe9\x97\xae" in buf):
                time.sleep(0.35)
                os.write(master_fd, b"/limits")
                sent = 1
                t_sent = time.monotonic()
            elif sent == 1 and time.monotonic() - t_sent > 0.25:
                enter_offset = len(buf)
                os.write(master_fd, b"\r")
                sent = 2
                t_sent = time.monotonic()
            elif sent == 2:
                post_buf = buf[enter_offset:]
                if (b"\xe6\xaf\x8f\xe6\x9c\x88" in post_buf or b"Monthly" in post_buf) and time.monotonic() - t_sent > 0.35:
                    time.sleep(0.15)
                    while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
                        r2, _, _ = select.select([master_fd], [], [], 0.05)
                        if not r2:
                            break
                        try:
                            c2 = os.read(master_fd, 16384)
                            if not c2:
                                break
                            buf = (buf + c2)[:_MAX_PROBE_OUTPUT]
                        except OSError:
                            break
                    break
                if time.monotonic() - t_sent > 3.5:
                    break
    except Exception:
        pass
    finally:
        if slave_fd >= 0:
            with contextlib.suppress(Exception):
                os.close(slave_fd)
        if proc is not None:
            _finish_probe(proc)
            master_fd = -1
        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if probe_slug_dir.exists():
            with contextlib.suppress(Exception):
                for f in list(probe_slug_dir.iterdir()):
                    if f not in pre_probe_files:
                        with contextlib.suppress(Exception):
                            f.unlink()
                for p in list(probe_slug_dir.glob("*.settings.json")):
                    jl = p.with_name(p.name[: -len(".settings.json")] + ".jsonl")
                    if jl.exists() and jl.stat().st_size < 450:
                        with contextlib.suppress(Exception):
                            p.unlink()
                        with contextlib.suppress(Exception):
                            jl.unlink()

    previous = dict(_DROID_QUOTA_CACHE)
    parsed_any = False
    if buf:
        after = buf[enter_offset:] if enter_offset > 0 else buf
        clean = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", b" ", after)
        txt = clean.decode("utf-8", errors="ignore")
        now_done = time.time()
        m_5h = re.search(r"(?:5\s*小时|5-hour|Five[\s-]*hour)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_5h:
            parsed_any = True
            u5 = float(m_5h.group(1))
            rsec5 = _parse_droid_reset_sec(m_5h.group(2))
            _DROID_QUOTA_CACHE["droid-5h"] = {
                "reset_precision_sec": droid_reset_precision(m_5h.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - u5) / 100.0)),
                "reset_ts": (now_done + rsec5) if rsec5 > 0 else 0.0,
            }
        m_wk = re.search(r"(?:每周|Weekly)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_wk:
            parsed_any = True
            uwk = float(m_wk.group(1))
            rsec_wk = _parse_droid_reset_sec(m_wk.group(2))
            _DROID_QUOTA_CACHE["droid-weekly"] = {
                "reset_precision_sec": droid_reset_precision(m_wk.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - uwk) / 100.0)),
                "reset_ts": (now_done + rsec_wk) if rsec_wk > 0 else 0.0,
            }
        m_mo = re.search(r"(?:每月|Monthly)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_mo:
            parsed_any = True
            umo = float(m_mo.group(1))
            rsec_mo = _parse_droid_reset_sec(m_mo.group(2))
            _DROID_QUOTA_CACHE["droid-monthly"] = {
                "reset_precision_sec": droid_reset_precision(m_mo.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - umo) / 100.0)),
                "reset_ts": (now_done + rsec_mo) if rsec_mo > 0 else 0.0,
            }

    if parsed_any:
        _LAST_DROID_SUCCESS_MTIME = max_s_mt
        _DROID_QUOTA_CACHE = anchor_droid_quota(_DROID_QUOTA_CACHE, previous, now_done)
    return {"droid-installed": {"installed": True}, **_DROID_QUOTA_CACHE}


def fetch_quota_buckets_with_meta(
    force_refresh: bool = False,
    seed_cache: dict[str, dict] | None = None,
    *,
    agy_force_refresh: bool | None = None,
    ui_active: bool = False,
) -> tuple[dict[str, dict], dict]:
    """Fetch user quota summary via official CLIs in parallel on demand."""
    eff_agy_force = force_refresh if agy_force_refresh is None else agy_force_refresh
    parsed_buckets: dict[str, dict] = {}
    agy_meta: dict = {"reason": "cached", "probed": False, "ok": True, "error": ""}

    def _run_agy() -> tuple[dict[str, dict], dict]:
        return fetch_agy_quota(
            cli_bin=_find_cli_bin("agy"),
            run_cmd=lambda cmd, timeout: _probe_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL),
            seed_cache=seed_cache,
            force_refresh=eff_agy_force,
            ui_active=ui_active,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="quota-probe") as pool:
        f_agy = pool.submit(_run_agy)
        f_codex = pool.submit(fetch_codex_quota, force_refresh)
        f_droid = pool.submit(fetch_droid_quota, seed_cache, force_refresh)
        f_grok = pool.submit(fetch_grok_quota, seed_cache, force_refresh)

        try:
            agy_buckets, agy_meta = f_agy.result()
            parsed_buckets.update(agy_buckets)
        except Exception as exc:
            agy_meta = {"reason": "error", "probed": bool(eff_agy_force), "ok": False, "error": str(exc) or type(exc).__name__}

        for fut in (f_codex, f_droid, f_grok):
            try:
                res = fut.result()
                if isinstance(res, dict):
                    parsed_buckets.update(res)
            except Exception:
                continue

    return parsed_buckets, agy_meta


def fetch_quota_buckets(
    force_refresh: bool = False,
    seed_cache: dict[str, dict] | None = None,
    *,
    agy_force_refresh: bool | None = None,
    ui_active: bool = False,
) -> dict[str, dict]:
    """Fetch user quota summary via official CLIs (AGY `/usage`, Codex sessions, Droid `/limits`, Grok stdio)."""
    parsed_buckets, _ = fetch_quota_buckets_with_meta(
        force_refresh=force_refresh,
        seed_cache=seed_cache,
        agy_force_refresh=agy_force_refresh,
        ui_active=ui_active,
    )
    return parsed_buckets


_GROK_IDLE_FORCE_PROBE_SEC = 900.0  # 15m idle sync cadence (within server 1800s stale window)


@serialized(lambda *args, **kwargs: "grok-quota")
def fetch_grok_quota(seed_cache: dict[str, dict] | None = None, force_refresh: bool = False) -> dict[str, dict]:
    """Fetch Grok (SuperGrok) weekly quota via `grok agent stdio` (`_x.ai/billing`) if `grok` is installed."""
    global _GROK_QUOTA_CACHE, _LAST_GROK_FETCH_TS, _LAST_GROK_SESSIONS_MTIME, _LAST_GROK_SUCCESS_MTIME
    grok_bin = _find_cli_bin("grok")
    if grok_bin is None:
        return {}

    if not _GROK_QUOTA_CACHE and seed_cache:
        for k in ("grok-weekly",):
            if isinstance(seed_cache.get(k), dict) and seed_cache[k].get("rem_frac") is not None:
                _GROK_QUOTA_CACHE[k] = dict(seed_cache[k])

    max_s_mt = 0.0
    if GROK_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(GROK_SESSIONS_DIR, "*/*/updates.jsonl"):
                try:
                    max_s_mt = max(max_s_mt, p.stat().st_mtime)
                except Exception:
                    continue
        except Exception:
            pass

    now = time.time()
    _LAST_GROK_SESSIONS_MTIME = max(_LAST_GROK_SESSIONS_MTIME, max_s_mt)
    if not force_refresh:
        return {"grok-installed": {"installed": True}, **_GROK_QUOTA_CACHE}

    _LAST_GROK_FETCH_TS = now
    _LAST_GROK_SESSIONS_MTIME = max(_LAST_GROK_SESSIONS_MTIME, max_s_mt)

    proc: subprocess.Popen | None = None
    parsed_ok = False
    try:
        proc = _start_probe(
            [grok_bin, "agent", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "agy-cockpit", "version": "1.0"},
            },
        }
        bill_req = {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}
        proc.stdin.write(json.dumps(init_req) + "\n" + json.dumps(bill_req) + "\n")
        proc.stdin.flush()

        fd = proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        buf = b""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
                    cfg = obj["result"].get("config") or {}
                    period = cfg.get("currentPeriod") or {}
                    end_str = str(period.get("end") or cfg.get("billingPeriodEnd") or "")
                    reset_ts = parse_iso_ts(end_str) or 0.0
                    raw_used = cfg.get("creditUsagePercent")
                    rem_frac = grok_remaining(raw_used)
                    if rem_frac is not None:
                        parsed_ok = True
                        _GROK_QUOTA_CACHE["grok-weekly"] = {
                            "rem_frac": rem_frac,
                            "reset_ts": reset_ts,
                            "observed_at": time.time(),
                            "period_start_ts": parse_iso_ts(period.get("start")) or 0.0,
                            "period_type": period.get("type"),
                        }
                    deadline = 0.0
                    break
    except Exception:
        pass
    finally:
        if proc is not None:
            _finish_probe(proc)

    if parsed_ok:
        _LAST_GROK_SUCCESS_MTIME = max_s_mt
    return {"grok-installed": {"installed": True}, **_GROK_QUOTA_CACHE}


def _norm_hex_color(val: object, default: str) -> str:
    if not isinstance(val, str):
        return default
    s = val.strip()
    if not s:
        return default
    if not s.startswith("#"):
        s = "#" + s
    if len(s) == 4:
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", s) else default


@serialized(lambda: "live-theme")
def get_live_theme() -> dict:
    """Read Antigravity userSettings theme seeds from ~/.gemini/config/config.json with mtime_ns caching."""
    global _THEME_CFG_MTIME_NS, _THEME_CFG_CACHE
    try:
        st = _AGY_CONFIG_FILE.stat()
        if st.st_mtime_ns == _THEME_CFG_MTIME_NS:
            return _THEME_CFG_CACHE
        raw = json.loads(_AGY_CONFIG_FILE.read_text(encoding="utf-8"))
        us = (raw.get("userSettings") if isinstance(raw, dict) else None) or {}
        raw_mode = us.get("themeMode")
        mode = "dark"
        if raw_mode == 2 or (isinstance(raw_mode, str) and "LIGHT" in raw_mode.upper()):
            mode = "light"
        elif raw_mode == 3 or (
            isinstance(raw_mode, str) and any(k in raw_mode.upper() for k in ("INHERIT", "SYSTEM", "AUTO"))
        ):
            mode = "system"

        d_seeds = us.get("customThemeSeedsDark") if isinstance(us.get("customThemeSeedsDark"), dict) else {}
        l_seeds = us.get("customThemeSeedsLight") if isinstance(us.get("customThemeSeedsLight"), dict) else {}

        _THEME_CFG_CACHE = {
            "mode": mode,
            "dark": {
                "background": _norm_hex_color(d_seeds.get("background"), "#101010"),
                "foreground": _norm_hex_color(d_seeds.get("foregroundOverride"), "#cccccc"),
                "primary": _norm_hex_color(d_seeds.get("primary"), "#007acc"),
            },
            "light": {
                "background": _norm_hex_color(l_seeds.get("background"), "#F9F9F9"),
                "foreground": _norm_hex_color(l_seeds.get("foregroundOverride"), "#101010"),
                "primary": _norm_hex_color(l_seeds.get("primary"), "#007acc"),
            },
        }
        _THEME_CFG_MTIME_NS = st.st_mtime_ns
    except Exception:
        pass
    return _THEME_CFG_CACHE
