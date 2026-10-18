"""A Floo chimney: a small Nostr relay (NIP-01) that only carries Floo messages.

    floo chimney                       # ws://127.0.0.1:7777, only reachable from this computer
    floo chimney --host 0.0.0.0        # reachable from your network (put a TLS proxy or tunnel in front for wss://)

It verifies every event's signature, only accepts the network's message kind, rate-limits each
connection and each author, stores events in SQLite and passes new ones to everyone subscribed.
"""
import asyncio
import collections
import json
import logging
import sqlite3
import time

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from . import nostr

log = logging.getLogger("floo.chimney")

MAX_FRAME = 128 * 1024        # largest websocket message accepted
MAX_SUBS = 20                 # open subscriptions per connection
MAX_FILTERS = 10              # filters per REQ
MAX_LIMIT = 500               # stored events returned per filter
EVENTS_PER_MINUTE = 30        # per connection, and separately per author
SOFTWARE = "floo-network chimney"


class RateLimiter:
    """Sliding one-minute window per key."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.hits = collections.defaultdict(collections.deque)

    def allow(self, key):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


def event_matches(flt, ev):
    """NIP-01 filter matching. All given conditions must hold."""
    if "ids" in flt and ev["id"] not in flt["ids"]:
        return False
    if "authors" in flt and ev["pubkey"] not in flt["authors"]:
        return False
    if "kinds" in flt and ev["kind"] not in flt["kinds"]:
        return False
    if "since" in flt and ev["created_at"] < flt["since"]:
        return False
    if "until" in flt and ev["created_at"] > flt["until"]:
        return False
    for key, wanted in flt.items():
        if len(key) == 2 and key[0] == "#":
            values = {t[1] for t in ev["tags"] if len(t) >= 2 and t[0] == key[1]}
            if not values & set(wanted):
                return False
    return True


def clean_filter(flt):
    """Keep only well-typed filter fields; anything odd makes the filter invalid (returns None)."""
    if not isinstance(flt, dict):
        return None
    out = {}
    for key, value in flt.items():
        if key in ("ids", "authors"):
            if not (isinstance(value, list) and len(value) <= 500 and all(isinstance(v, str) for v in value)):
                return None
            out[key] = value
        elif key == "kinds":
            if not (isinstance(value, list) and len(value) <= 50 and all(type(v) is int for v in value)):
                return None
            out[key] = value
        elif key in ("since", "until", "limit"):
            if type(value) is not int:
                return None
            out[key] = value
        elif len(key) == 2 and key[0] == "#" and key[1].isalpha():
            if not (isinstance(value, list) and len(value) <= 100 and all(isinstance(v, str) for v in value)):
                return None
            out[key] = value
        # unknown fields (e.g. NIP-50 "search") are ignored, as NIP-01 allows
    return out


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, pubkey TEXT, created_at INTEGER, kind INTEGER, raw TEXT, received INTEGER);
            CREATE INDEX IF NOT EXISTS events_time ON events(created_at);
            CREATE TABLE IF NOT EXISTS tags (event_id TEXT, name TEXT, value TEXT);
            CREATE INDEX IF NOT EXISTS tags_lookup ON tags(name, value);
        """)
        self.db.commit()

    def add(self, ev):
        """Store an event. Returns False if it was already stored."""
        cur = self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                              (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], json.dumps(ev), int(time.time())))
        if cur.rowcount == 0:
            return False
        self.db.executemany("INSERT INTO tags VALUES (?,?,?)",
                            [(ev["id"], t[0], t[1]) for t in ev["tags"] if len(t) >= 2 and len(t[0]) == 1])
        self.db.commit()
        return True

    def query(self, flt):
        where, args = [], []
        for col, key in (("id", "ids"), ("pubkey", "authors"), ("kind", "kinds")):
            if key in flt:
                if not flt[key]:
                    return []
                where.append(f"{col} IN ({','.join('?' * len(flt[key]))})")
                args += flt[key]
        if "since" in flt:
            where.append("created_at >= ?")
            args.append(flt["since"])
        if "until" in flt:
            where.append("created_at <= ?")
            args.append(flt["until"])
        for key, values in flt.items():
            if len(key) == 2 and key[0] == "#":
                if not values:
                    return []
                where.append(f"id IN (SELECT event_id FROM tags WHERE name=? AND value IN ({','.join('?' * len(values))}))")
                args += [key[1], *values]
        limit = max(0, min(flt.get("limit", MAX_LIMIT), MAX_LIMIT))
        sql = "SELECT raw FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [json.loads(r[0]) for r in self.db.execute(sql, (*args, limit))]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def prune(self, keep_days):
        cutoff = int(time.time()) - keep_days * 86400
        self.db.execute("DELETE FROM tags WHERE event_id IN (SELECT id FROM events WHERE created_at < ?)", (cutoff,))
        cur = self.db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount


class Chimney:
    def __init__(self, db_path, kinds, name="Floo chimney", description="", keep_days=None):
        self.store = Store(db_path)
        self.kinds = set(kinds)
        self.name, self.description = name, description
        self.keep_days = keep_days
        self.clients = {}  # websocket -> {sub_id: [filters]}
        self.per_conn = RateLimiter(EVENTS_PER_MINUTE)
        self.per_author = RateLimiter(EVENTS_PER_MINUTE)
        self.started = time.time()

    # ---------- HTTP: NIP-11 info document and a friendly page for browsers ----------

    def info(self):
        return {"name": self.name, "description": self.description or "A Floo Network chimney: carries signed Floo messages only.",
                "software": SOFTWARE, "supported_nips": [1, 11],
                "limitation": {"max_message_length": MAX_FRAME, "max_subscriptions": MAX_SUBS,
                               "max_filters": MAX_FILTERS, "max_limit": MAX_LIMIT,
                               "auth_required": False, "payment_required": False},
                "accepted_kinds": sorted(self.kinds)}

    def process_request(self, connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None  # carry on with the websocket handshake
        if "application/nostr+json" in request.headers.get("Accept", ""):
            body, ctype = json.dumps(self.info()), "application/nostr+json"
        else:
            body = (f"🔥 {self.name}\n\nThis is a Floo Network chimney (a Nostr relay for Floo messages).\n"
                    f"Connect with a websocket client, or add this address to a Portkey file.\n"
                    f"Events stored: {self.store.count()}\n")
            ctype = "text/plain; charset=utf-8"
        data = body.encode()
        headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(data))),
                           ("Access-Control-Allow-Origin", "*")])
        return Response(200, "OK", headers, data)

    # ---------- websocket protocol ----------

    async def send(self, ws, msg):
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass

    async def handler(self, ws):
        self.clients[ws] = {}
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    await self.send(ws, ["NOTICE", "invalid: not JSON"])
                    continue
                if not isinstance(msg, list) or not msg or not isinstance(msg[0], str):
                    await self.send(ws, ["NOTICE", "invalid: expected a JSON array"])
                    continue
                kind = msg[0]
                if kind == "EVENT" and len(msg) >= 2:
                    await self.on_event(ws, msg[1])
                elif kind == "REQ" and len(msg) >= 3:
                    await self.on_req(ws, msg[1], msg[2:])
                elif kind == "CLOSE" and len(msg) >= 2:
                    self.clients[ws].pop(msg[1], None)
                else:
                    await self.send(ws, ["NOTICE", f"invalid: unsupported message {kind!r}"])
        except Exception as e:  # dropped connections, oversized frames
            log.debug("connection ended: %s", e)
        finally:
            self.clients.pop(ws, None)

    async def on_event(self, ws, ev):
        eid = ev.get("id") if isinstance(ev, dict) and isinstance(ev.get("id"), str) else ""
        if not nostr.well_formed(ev):
            return await self.send(ws, ["OK", eid, False, "invalid: malformed event"])
        if ev["kind"] not in self.kinds:
            return await self.send(ws, ["OK", eid, False, f"blocked: this chimney only carries kind {sorted(self.kinds)}"])
        if not nostr.verify_event(ev):
            return await self.send(ws, ["OK", eid, False, "invalid: bad id or signature"])
        if not self.per_conn.allow(id(ws)) or not self.per_author.allow(ev["pubkey"]):
            return await self.send(ws, ["OK", eid, False, "rate-limited: slow down"])
        if not self.store.add(ev):
            return await self.send(ws, ["OK", eid, True, "duplicate: already have this event"])
        await self.send(ws, ["OK", eid, True, ""])
        for client, subs in list(self.clients.items()):
            for sub_id, filters in list(subs.items()):
                if any(event_matches(f, ev) for f in filters):
                    await self.send(client, ["EVENT", sub_id, ev])

    async def on_req(self, ws, sub_id, filters):
        if not isinstance(sub_id, str) or not 0 < len(sub_id) <= 64:
            return await self.send(ws, ["NOTICE", "invalid: subscription id must be 1-64 characters"])
        subs = self.clients[ws]
        if sub_id not in subs and len(subs) >= MAX_SUBS:
            return await self.send(ws, ["CLOSED", sub_id, f"rate-limited: at most {MAX_SUBS} subscriptions"])
        cleaned = [clean_filter(f) for f in filters[:MAX_FILTERS]]
        if any(f is None for f in cleaned):
            return await self.send(ws, ["CLOSED", sub_id, "invalid: bad filter"])
        subs[sub_id] = cleaned
        sent = set()
        for flt in cleaned:
            for ev in self.store.query(flt):
                if ev["id"] not in sent:
                    sent.add(ev["id"])
                    await self.send(ws, ["EVENT", sub_id, ev])
        await self.send(ws, ["EOSE", sub_id])

    # ---------- running ----------

    async def _prune_forever(self):
        while True:
            removed = self.store.prune(self.keep_days)
            if removed:
                log.info("pruned %d events older than %d days", removed, self.keep_days)
            await asyncio.sleep(3600)

    async def serve(self, host, port, ready=None):
        async with serve(self.handler, host, port, max_size=MAX_FRAME, process_request=self.process_request,
                         ping_interval=30, ping_timeout=30) as server:
            pruner = asyncio.create_task(self._prune_forever()) if self.keep_days else None
            if ready is not None:
                ready.set_result(server.sockets[0].getsockname()[1])
            try:
                await asyncio.Future()  # run until cancelled
            finally:
                if pruner:
                    pruner.cancel()


def default_db_path():
    from .hearth import FLOO_HOME
    path = FLOO_HOME / "chimney"
    path.mkdir(parents=True, exist_ok=True)
    return path / "events.db"
