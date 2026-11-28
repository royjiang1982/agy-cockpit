"""Validate reversible cost archives and isolate damage to individual files."""
from __future__ import annotations

import base64
import json
import math
import os
import time
import zlib
from collections import defaultdict
from datetime import date, datetime

from cost_schema import COMPACTED_UID, CostEvent, event_digest, event_digest_index

AGY_SOURCE_PREFIXES = ("db:", "agy_tr:")
ARCHIVE_MAPS = {"compacted_rows", "compacted_dates", "compacted_db_digests", "compacted_links", "compacted_zone"}
_BAD = object()
_SCALE = 100_000_000
_PACK_VERSION = 1  # ":" is outside the Base85 alphabet; unprefixed archives are legacy.


def _prefixes(rows):
    if isinstance(rows, CompactedRows):
        return (rows.digests[i:i+16] for i in range(0, len(rows.digests), 16))
    return (event_digest(row) for row in rows)


def agy_groups(files):
    """Form connected components so no digest or DB/transcript edge is cut."""
    parents = {key: key for key in files if key.startswith(AGY_SOURCE_PREFIXES)}
    owners = {}

    def root(key):
        while parents[key] != key:
            parents[key] = key = parents[parents[key]]
        return key

    for key in parents:
        if isinstance(files[key], CompactedRows):
            for peer in files[key].links:
                if peer in parents:
                    parents[root(peer)] = root(key)
        if not key.startswith("db:"):
            continue
        peer = "agy_tr:" + key[3:]
        if peer in parents:
            parents[root(peer)] = root(key)
        for digest in _prefixes(files[key]):
            if digest:
                parents[root(owners.setdefault(digest, key))] = root(key)
    groups = defaultdict(set)
    for key in parents:
        groups[root(key)].add(key)
    return groups.values()


def exact_rows(files, cutoffs=None, zone=None):
    """Use short digests only as an index. On a match, use original identities.

    Blindly skipping on hash/count would lose separate measured clock groups,
    or keep the wrong billed representative if a new source has a better clock.
    Restore the entire linked component and let the original deduplicator decide.
    """
    if not any(isinstance(rows, CompactedRows) for _, rows in files):
        return files
    mapped = dict(files)
    zone = timezone_key() if zone is None else zone
    unfold = {key for key, rows in files if isinstance(rows, CompactedRows)
              and (rows.zone != zone or (cutoffs and any(
                  row[0] >= cutoffs.get(key.partition(":")[0], math.inf) for row in rows)))}
    if any(isinstance(rows, CompactedRows) and key.startswith(AGY_SOURCE_PREFIXES) for key, rows in files):
        for group in agy_groups(mapped):
            folded = {key for key in group if isinstance(mapped[key], CompactedRows)}
            if folded and (folded != group or unfold & group or lost_peer(folded, mapped)):
                unfold.update(folded)
    return [(key, rows.original() if key in unfold else rows) for key, rows in files]


def lost_peer(folded, files):
    return any(peer not in files for key in folded for peer in files[key].links)

def timezone_key():
    # The historical timezone rules can change without changing today's offset.
    try:
        st = os.stat("/etc/localtime")
        local = st.st_dev, st.st_ino, st.st_mtime_ns
    except OSError:
        local = None
    return json.dumps((os.environ.get("TZ"), os.environ.get("TZDIR"), time.tzname,
                       time.timezone, time.altzone, time.daylight, local), separators=(",", ":"))


def cost_ticks(amount):
    """Costs already enter the cache rounded to eight decimal places."""
    return round(amount * _SCALE)


def cost_usd(ticks):
    return ticks / _SCALE


def calendar_cost(rows):
    return cost_usd(round(sum(row["_cost_ticks"] if "_cost_ticks" in row
                              else cost_ticks(row.get("cost", 0)) for row in rows), -2))


class NormalizedCostRows(list):
    """Rows already passed through the server's legacy normalization."""


class CompactedRows(NormalizedCostRows):
    """Summary rows plus enough information to recover exact inputs."""

    def __init__(self, rows, days, packed, digests, links, zone):
        super().__init__(rows)
        self.days = tuple(days)
        self.packed = packed
        self.digests = digests
        self.links = tuple(links)
        self.zone = zone

    def original(self):
        return unpack_rows(self.packed)


class ArchiveDamage(ValueError):
    """A folded file's exact inputs could not be recovered."""


def pack_rows(rows):
    hashes = bytearray()
    packed_rows = []
    for row in rows:
        head, sep, tail = row[3].partition("#")
        digest, at, clock = tail.partition("@")
        if sep and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            hashes.extend(bytes.fromhex(digest))
            row = (*row[:3], [head, at + clock], *row[4:])
        packed_rows.append(row)
    body = zlib.compress(json.dumps(packed_rows, separators=(",", ":")).encode())
    encoded = base64.b85encode(zlib.compress(len(hashes).to_bytes(4, "big") + hashes + body)).decode()
    return f"{_PACK_VERSION}:{encoded}"


def unpack_rows(packed):
    if not isinstance(packed, str) or not packed:
        raise ArchiveDamage("missing packed archive")
    try:
        version, sep, payload = packed.partition(":")
        if sep and version != str(_PACK_VERSION):
            raise ValueError("unsupported archive pack version")
        data = zlib.decompress(base64.b85decode(payload if sep else packed))
        size = int.from_bytes(data[:4], "big")
        if size % 32 or len(data) <= 4 + size:
            raise ValueError("invalid binary digest block")
        parts, payload = data[4:4+size], data[4+size:]
        rows = json.loads(zlib.decompress(payload))
        if not isinstance(rows, list):
            raise ValueError("expected array of original rows")
        hashes = iter(parts[i:i+32].hex() for i in range(0, len(parts), 32))
        original = NormalizedCostRows()
        lengths = (10,) if sep else (9, 10)
        for row in rows:
            if not isinstance(row, list) or len(row) not in lengths:
                raise ValueError("invalid archived row shape")
            if isinstance(row[3], list):
                head, suffix = row[3]
                row[3] = head + "#" + next(hashes) + suffix
            if not sep:
                row = [*row[:9], ""]  # v16 could tack on a local date, not a model.
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in row[:2])
                    or not isinstance(row[2], str) or not isinstance(row[3], str)
                    or row[3] == COMPACTED_UID or any(type(v) is not int for v in row[4:9])
                    or not isinstance(row[9], str)):
                raise ValueError("invalid archived row value")
            original.append(CostEvent(*row))
        if next(hashes, None) is not None:
            raise ValueError("unused archived digest")
        return original
    except (ValueError, TypeError, OverflowError, StopIteration, RecursionError, zlib.error) as exc:
        raise ArchiveDamage(f"invalid packed archive: {exc}") from exc


def persisted_compaction(files):
    maps = {name: {} for name in sorted(ARCHIVE_MAPS)}
    for key, (_, rows) in files.items():
        if isinstance(rows, CompactedRows):
            maps["compacted_dates"][key] = rows.days
            maps["compacted_rows"][key] = rows.packed
            maps["compacted_zone"][key] = rows.zone
            if rows.links:
                maps["compacted_links"][key] = rows.links
            if rows.digests:
                maps["compacted_db_digests"][key] = rows.digests
    return maps if maps["compacted_rows"] else {}


def _slot(snapshot, name, key, default=_BAD):
    mapping = snapshot.get(name)
    return mapping.get(key, default) if isinstance(mapping, dict) else _BAD


def _valid_zone(zone):
    try:
        tz, tzdir, names, regular, alternate, daylight, local = json.loads(zone)
        return (all(x is None or isinstance(x, str) for x in (tz, tzdir))
                and isinstance(names, list) and len(names) == 2 and all(isinstance(x, str) for x in names)
                and all(type(x) is int for x in (regular, alternate, daylight))
                and (local is None or (isinstance(local, list) and len(local) == 3
                                       and all(type(x) is int for x in local))))
    except (ValueError, TypeError, RecursionError):
        return False


def load_compacted_rows(key, rows, snapshot, *, zone=None):
    """Return validated summaries or the exact originals if metadata is suspect."""
    packed = _slot(snapshot, "compacted_rows", key)
    if (packed is _BAD and not any(row[3] == COMPACTED_UID for row in rows)
            and not any(_slot(snapshot, name, key) is not _BAD for name in ARCHIVE_MAPS)):
        return rows
    original = unpack_rows(packed)  # Validate now, not during startup aggregation.
    days = _slot(snapshot, "compacted_dates", key)
    digests = _slot(snapshot, "compacted_db_digests", key, "")
    links = _slot(snapshot, "compacted_links", key, ())
    archived_zone = _slot(snapshot, "compacted_zone", key)
    expected = event_digest_index(original) if key.startswith("db:") else ""
    valid_days = (isinstance(days, (list, tuple)) and len(days) == len(rows)
                  and all(type(d) is int and 1 <= d <= date.max.toordinal() for d in days))
    zone = timezone_key() if zone is None else zone
    if valid_days and archived_zone == zone:
        valid_days = all(datetime.fromtimestamp(row[0]).date().toordinal() == d
                         for row, d in zip(rows, days))
    if (not valid_days or digests != expected or not _valid_zone(archived_zone)
            or not isinstance(links, (list, tuple)) or not all(
                isinstance(peer, str) and peer != key and peer.startswith(("db:", "agy_tr:")) for peer in links)
            or not all(row[3] == COMPACTED_UID for row in rows)):
        return original
    return CompactedRows(rows, days, packed, digests, links, archived_zone)


def load_cached_files(raw, normalize, event_mtime, log):
    """Publish healthy files even if one entry is corrupt; preserve the disk copy."""
    files, damaged = {}, set()
    source = raw.get("files")
    if not isinstance(source, dict):
        log.error("cost cache: files missing or invalid; original snapshot will not be overwritten")
        return files, {None}, False
    wrapped = raw.get("compacted_rows")
    archive_keys = set().union(*(value for name in ARCHIVE_MAPS
                                 if isinstance(value := raw.get(name), dict)))
    repairs = False
    if (not isinstance(wrapped, dict) and not isinstance(raw.get("compacted_dates"), dict)
            and ("compacted_rows" in raw or archive_keys)):
        log.error("cost cache: compacted_rows missing or invalid; lost empty archives cannot be identified")
        damaged.add(None)
    for key in archive_keys - source.keys():
        log.error("cost cache: archive has no files entry for %r", key)
        damaged.add(key)
    zone = timezone_key()
    for key, value in source.items():
        try:
            mt, records = value
            if not isinstance(records, list):
                raise ValueError("expected an array of event rows")
            mtime = event_mtime(key, mt, raw.get("event_timing_rev"))
            if not math.isfinite(mtime):
                raise ValueError("non-finite file mtime")
        except (TypeError, ValueError, OverflowError) as exc:
            log.error("cost cache: cannot load %r: %s; retaining original snapshot", key, exc)
            damaged.add(key)
            continue
        rows = NormalizedCostRows()
        for record in records:
            try:
                row = normalize(record)
                if not all(math.isfinite(x) for x in row[:2]):
                    raise ValueError("non-finite event timestamp/cost")
                rows.append(row)
            except (TypeError, ValueError, IndexError, KeyError, OverflowError) as exc:
                log.error("cost cache: invalid event in %r: %s; retaining original snapshot", key, exc)
                damaged.add(key)
        try:
            loaded = load_compacted_rows(key, rows, raw, zone=zone)
            if loaded is not rows and not isinstance(loaded, CompactedRows):
                log.warning("cost cache: restored original rows for %r after invalid compaction metadata", key)
                repairs = True
                damaged.discard(key)  # A valid exact archive supersedes invalid summary rows.
            elif isinstance(loaded, CompactedRows):
                damaged.discard(key)
        except (ArchiveDamage, ValueError, OSError, OverflowError) as exc:
            log.error("cost cache: cannot recover %r: %s; queued rebuild, retaining original snapshot", key, exc)
            damaged.add(key)
            loaded = rows
        files[key] = (mtime, loaded)
    for key, (_, rows) in files.items():
        if isinstance(rows, CompactedRows):
            for peer in rows.links:
                other = files.get(peer)
                if other is not None and isinstance(other[1], CompactedRows) and key not in other[1].links:
                    log.warning("cost cache: invalid peer links for %r; restoring original rows", key)
                    repairs = True
                    break
    if repairs or damaged:
        # A missing peer edge might sever a historical fork group. Restore all
        # remaining AGY peers so their full identities, not the graph, decide.
        files = {key: (mt, rows.original() if isinstance(rows, CompactedRows)
                       and key.startswith(("db:", "agy_tr:")) else rows)
                 for key, (mt, rows) in files.items()}
    return files, damaged, repairs
