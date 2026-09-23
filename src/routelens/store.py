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

CREATE TABLE IF NOT EXISTS moderation_events (
    id             TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    session_source TEXT,
    ts             REAL NOT NULL,
    chain          TEXT NOT NULL,  -- 'input' | 'output'
    status         TEXT NOT NULL,  -- 'pass' | 'blocked' | 'error'
    categories     TEXT,           -- JSON list of flagged category names
    scores         TEXT,           -- JSON dict of category -> score
    reason         TEXT,
    latency_ms     REAL,
    preview        TEXT,
    key_alias      TEXT
);
-- A blocked-at-input request never gets an `attempts` row (the model is never called), so a
-- turn's *only* record can live here -- this table must be self-sufficient for rendering a turn,
-- not just a footnote joined onto `attempts`.
CREATE INDEX IF NOT EXISTS idx_modevents_session ON moderation_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_modevents_request ON moderation_events(request_id);
CREATE INDEX IF NOT EXISTS idx_modevents_ts ON moderation_events(ts);
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

MOD_COLUMNS = ["id", "request_id", "session_id", "session_source", "ts", "chain", "status",
               "categories", "scores", "reason", "latency_ms", "preview", "key_alias"]
_MOD_JSON_COLUMNS = ("categories", "scores")

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
            self._q.put_nowait(("attempts", row))
        except queue.Full:
            self.dropped += 1

    def record_moderation(self, row: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(("moderation_events", row))
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
        sqls = {
            "attempts": "INSERT OR REPLACE INTO attempts (%s) VALUES (%s)" % (
                ",".join(COLUMNS), ",".join("?" * len(COLUMNS))),
            "moderation_events": "INSERT OR REPLACE INTO moderation_events (%s) VALUES (%s)" % (
                ",".join(MOD_COLUMNS), ",".join("?" * len(MOD_COLUMNS))),
        }
        while True:
            item = self._q.get()
            batches: Dict[str, List[Dict[str, Any]]] = {"attempts": [], "moderation_events": []}
            events: List[threading.Event] = []
            n = 0
            while True:
                if item is _STOP:
                    conn.close()
                    return
                if isinstance(item, threading.Event):
                    events.append(item)
                else:
                    table, row = item
                    batches[table].append(row)
                    n += 1
                if n >= 200:
                    break
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
            try:
                if batches["attempts"]:
                    conn.executemany(sqls["attempts"], [self._to_params(r, COLUMNS, _JSON_COLUMNS)
                                                         for r in batches["attempts"]])
                if batches["moderation_events"]:
                    conn.executemany(sqls["moderation_events"], [self._to_params(r, MOD_COLUMNS, _MOD_JSON_COLUMNS)
                                                                  for r in batches["moderation_events"]])
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
    def _to_params(row: Dict[str, Any], columns: List[str], json_columns: tuple) -> list:
        out = []
        for c in columns:
            v = row.get(c)
            if c in json_columns and v is not None:
                v = json.dumps(v, default=str)
            out.append(v)
        return out

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = now - self.retention_days * 86400
        conn.execute("DELETE FROM attempts WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM moderation_events WHERE ts < ?", (cutoff,))
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
        # A row is either an `attempts` row or a `moderation_events` row, never both, so checking
        # the union of both tables' JSON column names against whichever columns this row actually
        # has is safe -- a missing key is just falsy and skipped.
        for c in _JSON_COLUMNS + _MOD_JSON_COLUMNS:
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

    def moderation_overview(self, since: float, bucket_s: int, chain: str = "all") -> Dict[str, Any]:
        params: tuple = (since,)
        where = "ts >= ?"
        if chain in ("input", "output"):
            where += " AND chain = ?"
            params = (since, chain)
        conn = self._connect()
        try:
            t = conn.execute(
                "SELECT COUNT(*) AS checked, COALESCE(SUM(status='blocked'),0) AS blocked, "
                "COALESCE(SUM(status='error'),0) AS errors, AVG(latency_ms) AS avg_latency_ms "
                "FROM moderation_events WHERE " + where, params).fetchone()
            by_chain = [dict(r) for r in conn.execute(
                "SELECT chain, COUNT(*) AS checked, COALESCE(SUM(status='blocked'),0) AS blocked "
                "FROM moderation_events WHERE ts >= ? GROUP BY chain", (since,))]
            cats: Dict[str, int] = {}
            for r in conn.execute("SELECT categories FROM moderation_events WHERE " + where +
                                   " AND status='blocked'", params):
                for c in (json.loads(r[0]) if r[0] else []):
                    cats[c] = cats.get(c, 0) + 1
            by_category = sorted(({"category": k, "n": v} for k, v in cats.items()), key=lambda x: -x["n"])
            series = [dict(r) for r in conn.execute(
                "SELECT CAST(ts / ? AS INTEGER) * ? AS t, COUNT(*) AS n, COALESCE(SUM(status='blocked'),0) AS blocked "
                "FROM moderation_events WHERE " + where + " GROUP BY t ORDER BY t",
                (bucket_s, bucket_s) + params)]
        finally:
            conn.close()
        checked = t["checked"] or 0
        return {
            "totals": {"checked": checked, "blocked": t["blocked"], "errors": t["errors"],
                       "block_rate": (t["blocked"] / checked) if checked else 0.0,
                       "avg_latency_ms": t["avg_latency_ms"]},
            "by_chain": by_chain, "by_category": by_category, "series": series,
        }

    def moderation_recent(self, limit: int = 40, chain: str = "all", blocked_only: bool = False) -> List[Dict[str, Any]]:
        where, params = [], []
        if chain in ("input", "output"):
            where.append("chain = ?"); params.append(chain)
        if blocked_only:
            where.append("status = 'blocked'")
        sql = "SELECT * FROM moderation_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return self._rows(sql, tuple(params))

    def moderation_for_requests(self, request_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Every moderation event for a set of turns, grouped by request_id -- used to merge
        into `session()`'s per-turn structure."""
        if not request_ids:
            return {}
        marks = ",".join("?" * len(request_ids))
        rows = self._rows(
            "SELECT * FROM moderation_events WHERE request_id IN (%s) ORDER BY ts" % marks,
            tuple(request_ids))
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["request_id"], []).append(r)
        return out

    def sessions(self, since: float, limit: int = 50, q: Optional[str] = None) -> List[Dict[str, Any]]:
        like = "%" + q + "%" if q else None
        a_where = "ts >= ?" + (" AND (session_id LIKE ? OR model LIKE ? OR preview LIKE ?)" if like else "")
        a_params: tuple = (since, like, like, like) if like else (since,)
        a_heads = self._rows(
            "SELECT session_id, session_source, COUNT(DISTINCT request_id) AS turns, "
            "MIN(ts) AS first_ts, MAX(ts) AS last_ts, COALESCE(SUM(cost),0) AS cost, "
            "COUNT(DISTINCT CASE WHEN attempt_no>0 THEN request_id END) AS rerouted, "
            "COALESCE(SUM(status='failure'),0) AS failed_attempts, MAX(key_alias) AS key_alias "
            "FROM attempts WHERE " + a_where + " GROUP BY session_id", a_params)

        # A request blocked at the input stage never gets an `attempts` row at all (the model is
        # never called), so a session made up *only* of blocked turns must still surface here --
        # merge in session identity from moderation_events too, not just attempts.
        m_where = "ts >= ?" + (" AND (session_id LIKE ? OR preview LIKE ?)" if like else "")
        m_params: tuple = (since, like, like) if like else (since,)
        m_heads = self._rows(
            "SELECT session_id, session_source, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "COALESCE(SUM(status='blocked'),0) AS blocked FROM moderation_events "
            "WHERE " + m_where + " GROUP BY session_id", m_params)

        merged: Dict[str, Dict[str, Any]] = {}
        for h in a_heads:
            merged[h["session_id"]] = dict(h, blocked=0)
        for h in m_heads:
            s = merged.get(h["session_id"])
            if s is None:
                merged[h["session_id"]] = {
                    "session_id": h["session_id"], "session_source": h["session_source"], "turns": 0,
                    "first_ts": h["first_ts"], "last_ts": h["last_ts"], "cost": 0.0, "rerouted": 0,
                    "failed_attempts": 0, "key_alias": None, "blocked": h["blocked"],
                }
            else:
                s["first_ts"] = min(s["first_ts"], h["first_ts"])
                s["last_ts"] = max(s["last_ts"], h["last_ts"])
                s["blocked"] = h["blocked"]

        heads = sorted(merged.values(), key=lambda s: -s["last_ts"])[:limit]
        if not heads:
            return []
        ids = [h["session_id"] for h in heads]
        marks = ",".join("?" * len(ids))
        path_rows = self._rows(
            "SELECT session_id, request_id, ts, provider, model, status FROM attempts "
            "WHERE session_id IN (%s) ORDER BY ts" % marks, tuple(ids))
        mod_rows = self._rows(
            "SELECT session_id, request_id, ts, chain, status FROM moderation_events "
            "WHERE session_id IN (%s) ORDER BY ts" % marks, tuple(ids))

        by_trace: Dict[str, Dict[str, Any]] = {}
        order: Dict[str, List[str]] = {}
        trace_ts: Dict[str, float] = {}
        seen: set = set()
        blocked_input: set = set()
        blocked_output: set = set()

        def note(sess_id: str, req_id: str, ts: float) -> None:
            trace_ts[req_id] = min(trace_ts.get(req_id, ts), ts)
            if req_id not in seen:
                seen.add(req_id)
                order.setdefault(sess_id, []).append(req_id)

        for r in mod_rows:
            note(r["session_id"], r["request_id"], r["ts"])
            if r["status"] == "blocked":
                (blocked_input if r["chain"] == "input" else blocked_output).add(r["request_id"])
        for r in path_rows:
            note(r["session_id"], r["request_id"], r["ts"])
            key = r["request_id"]
            cur = by_trace.get(key)  # last successful attempt wins; otherwise keep the last attempt
            if cur is None or r["status"] == "success" or cur["status"] != "success":
                by_trace[key] = r
        for sid in order:
            order[sid] = sorted(order[sid], key=lambda k: trace_ts.get(k, 0))

        for h in heads:
            path = []
            for k in order.get(h["session_id"], []):
                if k in blocked_input:
                    path.append({"provider": None, "model": None, "status": "blocked"})
                elif k in by_trace:
                    r = by_trace[k]
                    path.append({"provider": r["provider"], "model": r["model"],
                                 "status": "blocked" if k in blocked_output else r["status"]})
            h["path"] = path
            h["turns"] = len(path)
            h["switches"] = sum(1 for a, b in zip(path, path[1:]) if a["model"] != b["model"])
        return heads

    def session(self, session_id: str) -> Dict[str, Any]:
        rows = self._rows("SELECT * FROM attempts WHERE session_id = ? ORDER BY ts, attempt_no", (session_id,))
        mod_rows = self._rows("SELECT * FROM moderation_events WHERE session_id = ? ORDER BY ts", (session_id,))
        idx: Dict[str, Dict[str, Any]] = {}

        def turn_for(request_id: str, ts: float) -> Dict[str, Any]:
            t = idx.get(request_id)
            if t is None:
                t = {"request_id": request_id, "ts": ts, "attempts": [],
                     "moderation": {"input": None, "output": None}}
                idx[request_id] = t
            else:
                t["ts"] = min(t["ts"], ts)
            return t

        for r in rows:
            turn_for(r["request_id"], r["ts"])["attempts"].append(r)
        for r in mod_rows:
            turn_for(r["request_id"], r["ts"])["moderation"][r["chain"]] = r

        turns = sorted(idx.values(), key=lambda t: t["ts"])
        for i, t in enumerate(turns):
            t["turn"] = i + 1
            ok = [a for a in t["attempts"] if a["status"] == "success"]
            blocked_in = t["moderation"]["input"] and t["moderation"]["input"]["status"] == "blocked"
            blocked_out = t["moderation"]["output"] and t["moderation"]["output"]["status"] == "blocked"
            if t["attempts"]:
                t["final"] = ok[-1] if ok else t["attempts"][-1]
                t["status"] = "blocked" if (blocked_in or blocked_out) else ("success" if ok else "failure")
            else:
                # No model was ever called -- either blocked at input, or (defensively) some
                # other gap. Never crash the dashboard over it either way.
                t["final"] = None
                t["status"] = "blocked" if blocked_in else "unknown"
            t["cost"] = sum(a["cost"] or 0 for a in t["attempts"])
            t["latency_ms"] = sum(a["latency_ms"] or 0 for a in t["attempts"])
            t["preview"] = (next((a["preview"] for a in t["attempts"] if a.get("preview")), None)
                             or (t["moderation"]["input"] or {}).get("preview"))
        return {
            "session_id": session_id,
            "session_source": (rows[0]["session_source"] if rows else
                                (mod_rows[0]["session_source"] if mod_rows else None)),
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
