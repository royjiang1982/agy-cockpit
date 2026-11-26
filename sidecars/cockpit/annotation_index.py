"""Incremental App/CLI annotation index; analyzer serializes calls to it."""
from __future__ import annotations

import contextlib
import time


class AnnotationIndex:
    TTL = 0.2

    def __init__(self):
        self._snapshot = None
        self._read_at = 0.0
        self._files = {}

    def read(self, app_dirs, cached_paths, parse, titles_type, db_mtime, connect):
        now = time.monotonic()
        cached = self._snapshot
        if cached is not None and now - self._read_at < self.TTL:
            return cached[1:]
        self._read_at = now
        sig_parts, sources = [], []
        for _, base in app_dirs:
            entries = []
            ann_dir = base / 'annotations'
            if ann_dir.exists():
                for path in cached_paths(ann_dir, '*.pbtxt', ttl=self.TTL):
                    try:
                        mtime = path.stat().st_mtime_ns
                        sig_parts.append((str(path), mtime))
                    except OSError:
                        mtime = None
                    entries.append((path, mtime))
            db = base / 'conversation_summaries.db'
            if db.exists():
                with contextlib.suppress(Exception):
                    sig_parts.append((str(db), int(db_mtime(db) * 1e9)))
            sources.append((str(base), entries, db))
        signature = tuple(sorted(sig_parts))
        if cached is not None and cached[0] == signature:
            return cached[1:]

        titles, view_times, mtimes, parsed = titles_type(), {}, {}, {}
        previous = cached[1].by_source if cached else {}
        for base, entries, db in sources:
            for path, mtime in entries:
                cid = path.stem
                try:
                    if mtime is None:
                        raise OSError('annotation disappeared during discovery')
                    if mtime > mtimes.get(cid, 0):
                        mtimes[cid] = mtime
                    key = (path, mtime)
                    annotation = self._files.get(key)
                    if annotation is None:
                        annotation = parse(path.read_text(encoding='utf-8'))
                    parsed[key] = annotation
                    titles.by_source[base, cid] = annotation
                    if annotation.get('title') and cid not in titles:
                        titles[cid] = annotation['title']
                    stamp = annotation.get('last_user_view_time')
                    if isinstance(stamp, dict):
                        vt = int(stamp.get('seconds', 0)) + int(stamp.get('nanos', 0)) / 1e9
                        if vt > view_times.get(cid, 0.0):
                            view_times[cid] = vt
                except Exception:
                    # Do not resurrect an archived item on a malformed rewrite.
                    # Failed reads can be retried on the next signature change.
                    prior = dict(previous.get((base, cid), {}))
                    prior['annotation_error'] = True
                    titles.by_source[base, cid] = prior
            if db.exists():
                with contextlib.suppress(Exception), contextlib.closing(connect(db, timeout=0.3)) as con:
                    for cid, title in con.execute('SELECT conversation_id, title FROM conversation_summaries'):
                        if title and cid not in titles:
                            titles[cid] = title
        self._files = parsed
        self._snapshot = signature, titles, view_times, mtimes
        return titles, view_times, mtimes
