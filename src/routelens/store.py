"""SQLite storage for routing decisions.

Writes go through a single background thread so the proxy's event loop never
blocks on disk. Reads open short-lived connections (WAL mode allows this).
"""
from __future__ import annotations

import json
import math
import queue
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    call_id          TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    session_source   TEXT,
    ts               REAL NOT NULL,
    attempt_no       INTEGER DEFAULT 0,
    requested_model  TEXT,
    model_group      TEXT,
    deployment_id    TEXT,
    model            TEXT,
    provider         TEXT,
    status           TEXT NOT NULL,
    latency_ms       REAL,
    cost             REAL DEFAULT 0,
    prompt_tokens    INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cache_hit        INTEGER DEFAULT 0,
    strategy         TEXT,
    reason_kind      TEXT,
    reason           TEXT,
    reason_detail    TEXT,
    candidates       TEXT,
    excluded         TEXT,
    signals          TEXT,
    error_class      TEXT,
    error_code       TEXT,
    error            TEXT,
    end_user         TEXT,
    key_alias        TEXT,
    tags             TEXT,
    preview          TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_attempts_trace ON attempts(request_id);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
"""

COLUMNS = [
    "call_id", "request_id", "session_id", "session_source", "ts", "attempt_no",
    "requested_model", "model_group", "deployment_id", "model", "provider",
    "status", "latency_ms", "cost", "prompt_tokens", "completion_tokens",
    "cache_hit", "strategy", "reason_kind", "reason", "reason_detail",
    "candidates", "excluded", "signals", "error_class", "error_code", "error",
    "end_user", "key_alias", "tags", "preview",
]
_JSON_COLUMNS = ("reason_detail", "candidates", "excluded", "signals", "tags")

_STOP = object()


class Store:
    def __init__(self, path: str, retention_days: float = 14.0) -> None:
        self.path = path
        self.retention_days = retention_days
        self.dropped = 0
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=20000)
        self._last_prune = 0.0
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self._thread = threading.Thread(target=self._run, name="routelens-writer", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ write
    def record(self, row: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Block until everything queued so far is on disk (used by tests/CLI)."""
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _run(self) -> None:
        conn = self._connect()
        sql = "INSERT OR REPLACE INTO attempts (%s) VALUES (%s)" % (
            ",".join(COLUMNS), ",".join("?" * len(COLUMNS)))
        while True:
            item = self._q.get()
            batch: List[Dict[str, Any]] = []
            events: List[threading.Event] = []
            while True:
                if item is _STOP:
                    conn.close()
                    return
                if isinstance(item, threading.Event):
                    events.append(item)
                else:
                    batch.append(item)
                if len(batch) >= 200:
                    break
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
            try:
                if batch:
                    conn.executemany(sql, [self._to_params(r) for r in batch])
                    conn.commit()
                self._maybe_prune(conn)
            except Exception:  # never let the writer thread die
                try:
                    conn.rollback()
                except Exception:
                    pass
            for e in events:
                e.set()

    @staticmethod
    def _to_params(row: Dict[str, Any]) -> list:
        out = []
        for c in COLUMNS:
            v = row.get(c)
            if c in _JSON_COLUMNS and v is not None:
                v = json.dumps(v, default=str)
            out.append(v)
        return out

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        conn.execute("DELETE FROM attempts WHERE ts < ?", (now - self.retention_days * 86400,))
        conn.commit()

    def close(self) -> None:
        self.flush()
        self._q.put(_STOP)
        self._thread.join(timeout=5)

    # ------------------------------------------------------------------- read
    def _rows(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            return [self._from_row(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _from_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for c in _JSON_COLUMNS:
            if d.get(c):
                try:
                    d[c] = json.loads(d[c])
                except ValueError:
                    pass
        return d

    def providers(self) -> List[str]:
        """Providers in first-seen order. Colour follows the entity, so this order is stable."""
        rows = self._rows(
            "SELECT provider, MIN(ts) AS first FROM attempts WHERE provider IS NOT NULL "
            "GROUP BY provider ORDER BY first")
        return [r["provider"] for r in rows]

    def overview(self, since: float, bucket_s: int) -> Dict[str, Any]:
        conn = self._connect()
        try:
            t = conn.execute(
                "SELECT COUNT(DISTINCT request_id) AS requests, COUNT(DISTINCT session_id) AS sessions, "
                "COUNT(*) AS attempts, COALESCE(SUM(status='failure'),0) AS failed_attempts, "
                "COUNT(DISTINCT CASE WHEN attempt_no>0 THEN request_id END) AS rerouted, "
                "COALESCE(SUM(cost),0) AS cost FROM attempts WHERE ts >= ?", (since,)).fetchone()
            lat = [r[0] for r in conn.execute(
                "SELECT latency_ms FROM attempts WHERE ts >= ? AND status='success' AND latency_ms IS NOT NULL "
                "ORDER BY latency_ms LIMIT 100000", (since,))]
            by_model = [dict(r) for r in conn.execute(
                "SELECT provider, model, COUNT(*) AS n, COALESCE(SUM(cost),0) AS cost, AVG(latency_ms) AS avg_latency_ms "
                "FROM attempts WHERE ts >= ? AND status='success' GROUP BY provider, model ORDER BY n DESC LIMIT 12",
                (since,))]
            by_reason = [dict(r) for r in conn.execute(
                "SELECT reason_kind AS kind, COUNT(*) AS n FROM attempts WHERE ts >= ? "
                "GROUP BY reason_kind ORDER BY n DESC", (since,))]
            series = [dict(r) for r in conn.execute(
                "SELECT CAST(ts / ? AS INTEGER) * ? AS t, COUNT(*) AS n, COALESCE(SUM(status='failure'),0) AS failed "
                "FROM attempts WHERE ts >= ? GROUP BY t ORDER BY t", (bucket_s, bucket_s, since))]
        finally:
            conn.close()
        return {
            "totals": {
                "requests": t["requests"], "sessions": t["sessions"], "attempts": t["attempts"],
                "failed_attempts": t["failed_attempts"], "rerouted": t["rerouted"], "cost": t["cost"],
                "p50_ms": _pct(lat, 0.50), "p95_ms": _pct(lat, 0.95),
            },
            "by_model": by_model, "by_reason": by_reason, "series": series,
        }

    def sessions(self, since: float, limit: int = 50, q: Optional[str] = None) -> List[Dict[str, Any]]:
        like = "%" + q + "%" if q else None
        where = "ts >= ?" + (" AND (session_id LIKE ? OR model LIKE ? OR preview LIKE ?)" if like else "")
        params: tuple = (since, like, like, like) if like else (since,)
        heads = self._rows(
            "SELECT session_id, MAX(session_source) AS session_source, COUNT(DISTINCT request_id) AS turns, "
            "MIN(ts) AS first_ts, MAX(ts) AS last_ts, COALESCE(SUM(cost),0) AS cost, "
            "COUNT(DISTINCT CASE WHEN attempt_no>0 THEN request_id END) AS rerouted, "
            "COALESCE(SUM(status='failure'),0) AS failed_attempts, MAX(key_alias) AS key_alias "
            "FROM attempts WHERE " + where + " GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
            params + (limit,))
        if not heads:
            return []
        ids = [h["session_id"] for h in heads]
        marks = ",".join("?" * len(ids))
        path_rows = self._rows(
            "SELECT session_id, request_id, ts, provider, model, status FROM attempts "
            "WHERE session_id IN (%s) ORDER BY ts" % marks, tuple(ids))
        by_trace: Dict[str, Dict[str, Any]] = {}
        order: Dict[str, List[str]] = {}
        for r in path_rows:
            key = r["request_id"]
            if key not in by_trace:
                order.setdefault(r["session_id"], []).append(key)
            # last successful attempt wins; otherwise keep the last attempt
            cur = by_trace.get(key)
            if cur is None or r["status"] == "success" or cur["status"] != "success":
                by_trace[key] = r
        for h in heads:
            h["path"] = [
                {"provider": by_trace[k]["provider"], "model": by_trace[k]["model"], "status": by_trace[k]["status"]}
                for k in order.get(h["session_id"], [])
            ]
            h["switches"] = sum(
                1 for a, b in zip(h["path"], h["path"][1:]) if a["model"] != b["model"])
        return heads

    def session(self, session_id: str) -> Dict[str, Any]:
        rows = self._rows("SELECT * FROM attempts WHERE session_id = ? ORDER BY ts, attempt_no", (session_id,))
        turns: List[Dict[str, Any]] = []
        idx: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            t = idx.get(r["request_id"])
            if t is None:
                t = {"request_id": r["request_id"], "turn": len(turns) + 1, "ts": r["ts"], "attempts": []}
                idx[r["request_id"]] = t
                turns.append(t)
            t["attempts"].append(r)
        for t in turns:
            ok = [a for a in t["attempts"] if a["status"] == "success"]
            final = ok[-1] if ok else t["attempts"][-1]
            t["final"] = final
            t["status"] = "success" if ok else "failure"
            t["cost"] = sum(a["cost"] or 0 for a in t["attempts"])
            t["latency_ms"] = sum(a["latency_ms"] or 0 for a in t["attempts"])
            t["preview"] = next((a["preview"] for a in t["attempts"] if a.get("preview")), None)
        return {
            "session_id": session_id,
            "session_source": rows[0]["session_source"] if rows else None,
            "turns": turns,
        }

    def recent(self, limit: int = 40) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM attempts ORDER BY ts DESC LIMIT ?", (limit,))


def _pct(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    # nearest-rank: the smallest value with at least p of the data at or below it
    i = min(len(sorted_vals) - 1, max(0, math.ceil(p * len(sorted_vals)) - 1))
    return sorted_vals[i]
