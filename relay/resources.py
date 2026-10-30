"""Bounded, asynchronous resource diagnostics. Never read argv or credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import threading
import time


MAX_PROCESSES = 64
MAX_FDS = 1024


def _text(path, maximum=16384):
    try:
        with path.open(encoding="utf-8") as handle:
            value = handle.read(maximum + 1)
        return value.strip() if len(value) <= maximum else None
    except (OSError, UnicodeError):
        return None


def _number(value):
    return int(value) if value is not None and value.isascii() and value.isdecimal() else None


def _limit(path):
    value = _text(path)
    return {"value": _number(value), "unlimited": True if value == "max" else False if _number(value) is not None else None}


def _process(path):
    raw = _text(path / "status")
    if raw is None:
        return None
    fields = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
    rss = fields.get("VmRSS", "").strip().split()
    resident = _number(rss[0]) * 1024 if len(rss) == 2 and rss[1] == "kB" and _number(rss[0]) is not None else None
    count = None
    truncated = False
    try:
        with os.scandir(path / "fd") as entries:
            count = 0
            for _ in entries:
                if count == MAX_FDS:
                    truncated = True
                    break
                count += 1
    except OSError:
        count = None
    return {"pid": int(path.name), "parent_pid": _number(fields.get("PPid", "").strip()),
            "name": "".join(c for c in fields.get("Name", "").strip() if c.isprintable())[:80],
            "resident_bytes": resident, "threads": _number(fields.get("Threads", "").strip()),
            "open_fds": count, "open_fds_truncated": truncated}


def collect_resources(data_directory, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    """Read the current cgroup/namespace; per-process RSS is never summed."""
    proc_root, cgroup_root = Path(proc_root), Path(cgroup_root)
    processes, scan_available, scan_truncated = [], False, False
    try:
        # Stop directory enumeration before an unbounded host process listing.
        scanned = 0
        with os.scandir(proc_root) as entries:
            for entry in entries:
                if not entry.name.isascii() or not entry.name.isdecimal():
                    continue
                if scanned == MAX_PROCESSES:
                    scan_truncated = True
                    break
                scanned += 1
                value = _process(proc_root / entry.name)
                if value is not None:
                    processes.append(value)
        scan_available = True
    except OSError:
        pass
    memory = _number(_text(cgroup_root / "memory.current"))
    events = {}
    for line in (_text(cgroup_root / "memory.events") or "").splitlines():
        values = line.split()
        if len(values) == 2 and values[0] in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}:
            events[values[0]] = _number(values[1])
    memory_limit, pids_limit = _limit(cgroup_root / "memory.max"), _limit(cgroup_root / "pids.max")
    cpu = {}
    for line in (_text(cgroup_root / "cpu.stat") or "").splitlines():
        values = line.split()
        if len(values) == 2 and values[0] in {"usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"}:
            cpu[values[0]] = _number(values[1])
    container = {"memory_current_bytes": memory, "memory_peak_bytes": _number(_text(cgroup_root / "memory.peak")),
                 "memory_limit_bytes": memory_limit["value"], "memory_limit_unlimited": memory_limit["unlimited"],
                 "memory_events": events, "cpu_counters": cpu,
                 "pids_current": _number(_text(cgroup_root / "pids.current")),
                 "pids_limit": pids_limit["value"], "pids_limit_unlimited": pids_limit["unlimited"]}
    storage = None
    try:
        usage = shutil.disk_usage(data_directory)
        storage = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError:
        pass
    return {"available": scan_available or memory is not None or storage is not None,
            "collected_at": datetime.now(timezone.utc).isoformat(), "container": container,
            "processes_available": scan_available, "processes_truncated": scan_truncated,
            "processes": sorted(processes, key=lambda item: item["pid"]), "data_filesystem": storage}


class ResourceDiagnostics:
    """At most one collector thread; status requests never wait for filesystem IO."""

    def __init__(self, data_directory, *, ttl=10, collect=collect_resources, clock=time.monotonic):
        self.directory, self.ttl, self.collect, self.clock = data_directory, ttl, collect, clock
        self._lock = threading.Lock()
        self._value = {"available": False}
        self._sampled = None
        self._attempted = None
        self._running = False
        self._error = None

    def _refresh(self):
        try:
            value = self.collect(self.directory)
            with self._lock:
                self._value, self._sampled, self._error = value, self.clock(), None
        except Exception as exc:
            with self._lock:
                self._error = type(exc).__name__  # Never expose exception text or paths.
        finally:
            with self._lock:
                self._running = False

    def snapshot(self):
        with self._lock:
            now = self.clock()
            age = None if self._sampled is None else max(0, now - self._sampled)
            last = max((value for value in (self._attempted, self._sampled) if value is not None), default=None)
            if not self._running and (last is None or now - last >= self.ttl):
                self._attempted = now
                self._running = True
                try:
                    threading.Thread(target=self._refresh, name="resource-diagnostics", daemon=True).start()
                except RuntimeError as exc:
                    self._running, self._error = False, type(exc).__name__
            return dict(self._value, collecting=self._running, age_seconds=age,
                        stale=age is None or age >= self.ttl, collection_error=self._error)
