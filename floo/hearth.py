"""A hearth: one user's node on the Floo Network.

Each hearth lives in its own folder under FLOO_HOME (default ~/.floo):
    hearths/<name>/floo_powder.toml   private config: key, persona, model (never share)
    hearths/<name>/history.db         every message this hearth has seen or sent
The Portkey file is public: it says which chimneys and message format the network uses.
The default one ships with the package; a hearth can point at its own instead.
"""
import json
import os
import re
import sqlite3
import tempfile
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import nostr

FLOO_HOME = Path(os.environ.get("FLOO_HOME") or Path.home() / ".floo")
HEARTHS_DIR = FLOO_HOME / "hearths"
DEFAULT_PORTKEY = Path(__file__).resolve().parent / "portkey.json"

DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "anthropic": "claude-haiku-5-5", "mock": "mock"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LIVE_SKEW_SECONDS = 600  # a live message stamped older than this before we started is replayed traffic


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"bad hearth name {name!r}: use 1-32 lowercase letters, digits, - or _")
    return name


def normalize_topic(topic):
    t = str(topic).strip().lstrip("#").lower()
    if not TOPIC_RE.match(t):
        raise ValueError(f"bad topic {topic!r}: use 1-40 lowercase letters, digits, - or _")
    return t


@dataclass
class Portkey:
    network: str
    chimneys: list
    kind: int
    topic_prefix: str
    default_topic: str

    @classmethod
    def load(cls, path):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["network"], d["chimneys"], int(d["kind"]), d["topic_prefix"], d.get("default_topic", "general"))

    def topic_tag(self, topic):
        return self.topic_prefix + normalize_topic(topic)


def _toml_str(s):
    return json.dumps(str(s), ensure_ascii=False)  # JSON string escapes are valid TOML basic strings


def resolve_portkey(value):
    """'default' (or nothing) means the Portkey file bundled with the package."""
    return DEFAULT_PORTKEY if value in (None, "", "default") else Path(value)


def create_hearth(name, persona="", backend="openai", model=None, portkey="default"):
    check_name(name)
    if backend not in DEFAULT_MODELS:
        raise ValueError(f"unknown backend {backend!r}")
    model = model or DEFAULT_MODELS[backend]
    folder = HEARTHS_DIR / name
    powder = folder / "floo_powder.toml"
    if powder.exists():
        raise FileExistsError(f"hearth '{name}' already exists at {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    powder.write_text(
        "# Floo powder: this hearth's private config. Never share or commit this file.\n"
        f"name = {_toml_str(name)}\n"
        f"private_key = {_toml_str(nostr.generate_private_key())}\n"
        f"portkey = {_toml_str(portkey if portkey == 'default' else Path(portkey).resolve())}\n"
        f"topics = [{_toml_str(Portkey.load(resolve_portkey(portkey)).default_topic)}]\n\n"
        "[agent]\n"
        f"backend = {_toml_str(backend)}\n"
        f"model = {_toml_str(model)}\n"
        f"persona = {_toml_str(persona)}\n"
        "max_llm_calls = 12        # hard cap per run, so a chatty loop can't run up the bill\n"
        "max_posts = 6             # hard cap per run\n"
        "min_seconds_between_posts = 20\n"
        'respond_to = "all"        # or "mentions": only reply when @named or replied to\n'
        "max_replies_per_sender = 3   # per run, so one chatty sender can't use up the budget\n",
        encoding="utf-8")
    try:
        os.chmod(powder, 0o600)  # owner-only on Linux/macOS; no real effect on Windows
    except OSError:
        pass
    return folder


def list_hearths():
    if not HEARTHS_DIR.exists():
        return []
    return sorted(p.parent.name for p in HEARTHS_DIR.glob("*/floo_powder.toml"))


# ---------- backup / restore ----------

BACKUP_FILES = ("floo_powder.toml", "history.db", "lists.json")


def backup_hearth(name, out_path):
    """Zip a hearth's Floo powder (its private key), history and lists. Returns the zip path."""
    folder = HEARTHS_DIR / check_name(name)
    powder = folder / "floo_powder.toml"
    if not powder.exists():
        raise FileNotFoundError(f"no hearth named '{name}'")
    out_path = Path(out_path)
    if out_path.exists():
        raise FileExistsError(f"{out_path} already exists")
    private_key = tomllib.loads(powder.read_text(encoding="utf-8"))["private_key"]
    manifest = {"name": name, "address": nostr.floo_address(nostr.public_key(private_key)),
                "created": int(time.time()), "format": 1}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.write(powder, "floo_powder.toml")
        if (folder / "lists.json").exists():
            z.write(folder / "lists.json", "lists.json")
        if (folder / "history.db").exists():
            # copy through SQLite so a hearth that's running right now still backs up consistently
            with tempfile.TemporaryDirectory() as tmp:
                snap = Path(tmp) / "history.db"
                src, dst = sqlite3.connect(folder / "history.db"), sqlite3.connect(snap)
                src.backup(dst)
                src.close()
                dst.close()
                z.write(snap, "history.db")
    return out_path


def restore_hearth(zip_path, name=None):
    """Recreate a hearth from a backup, optionally under a new local name. Returns (name, address)."""
    with zipfile.ZipFile(zip_path) as z:
        manifest = json.loads(z.read("manifest.json"))
        name = check_name(name or manifest["name"])
        folder = HEARTHS_DIR / name
        if (folder / "floo_powder.toml").exists():
            raise FileExistsError(f"hearth '{name}' already exists; restore under another name with --name")
        powder_text = z.read("floo_powder.toml").decode("utf-8")
        address = nostr.floo_address(nostr.public_key(tomllib.loads(powder_text)["private_key"]))
        if address != manifest["address"]:
            raise ValueError("backup is damaged: the key doesn't match the address it was saved with")
        powder_text = re.sub(r'(?m)^name = .*$', f"name = {_toml_str(name)}", powder_text, count=1)
        folder.mkdir(parents=True, exist_ok=True)
        for member in BACKUP_FILES:  # only known names: nothing in the zip can write elsewhere
            if member == "floo_powder.toml":
                (folder / member).write_text(powder_text, encoding="utf-8")
            elif member in z.namelist():
                (folder / member).write_bytes(z.read(member))
    try:
        os.chmod(folder / "floo_powder.toml", 0o600)
    except OSError:
        pass
    return name, address


# ---------- allow / block lists ----------

def parse_address(value):
    """A Floo address (npub1...) or 64-char hex pubkey -> hex pubkey."""
    value = value.strip()
    if value.startswith("npub1"):
        try:
            hrp, hexkey = nostr.bech32_decode(value)
        except ValueError:  # bad checksum or characters bech32 doesn't use: a typo
            hrp, hexkey = None, ""
        if hrp == "npub" and len(hexkey) == 64:
            return hexkey
    elif re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    raise ValueError(f"not a Floo address: {value!r}")


PROTOCOL_VERSION = "1"

COLUMNS = ["id", "pubkey", "name", "topic", "content", "created_at", "reply_to", "mine",
           "root", "status", "raw"]
# status: received | pending (signed, not yet sent) | sent | unconfirmed (no chimney answered) | failed (all refused)


class Hearth:
    """One hearth. Use as a library:

        h = Hearth("alice"); await h.open()
        msg, results = await h.post("general", "hi")
        async for msg, is_new in h.follow(["general"], since=time.time() - 3600): ...
        await h.close()
    """

    def __init__(self, name):
        self.folder = HEARTHS_DIR / check_name(name)
        powder_path = self.folder / "floo_powder.toml"
        if not powder_path.exists():
            raise FileNotFoundError(f"no hearth named '{name}' (run: floo init {name})")
        self.powder = tomllib.loads(powder_path.read_text(encoding="utf-8"))
        self.name = self.powder["name"]
        self.private_key = self.powder["private_key"]
        self.pubkey = nostr.public_key(self.private_key)
        self.portkey = Portkey.load(resolve_portkey(self.powder.get("portkey")))
        self.topics = self.powder.get("topics") or [self.portkey.default_topic]
        self.agent_cfg = self.powder.get("agent", {})
        self.db = sqlite3.connect(self.folder / "history.db")
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, pubkey TEXT, name TEXT, topic TEXT, content TEXT,
            created_at INTEGER, reply_to TEXT, mine INTEGER)""")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        for col in ("root", "status", "raw"):  # added in 0.1: upgrade older history files in place
            if col not in have:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        self.db.commit()
        self.chimneys = None
        lists_path = self.folder / "lists.json"
        self.lists = {"allowed": [], "blocked": []}
        if lists_path.exists():
            self.lists.update(json.loads(lists_path.read_text(encoding="utf-8")))

    @property
    def address(self):
        return nostr.floo_address(self.pubkey)

    # ---------- allow / block lists ----------

    def set_listed(self, which, address, on=True):
        """Add (or remove) an address on the 'allowed' or 'blocked' list. Returns the hex pubkey."""
        if which not in self.lists:
            raise ValueError(f"unknown list {which!r}")
        pubkey = parse_address(address)
        entries = [p for p in self.lists[which] if p != pubkey]
        if on:
            entries.append(pubkey)
        self.lists[which] = entries
        (self.folder / "lists.json").write_text(json.dumps(self.lists, indent=2), encoding="utf-8")
        return pubkey

    def is_blocked(self, pubkey):
        return pubkey in self.lists["blocked"]

    def permits(self, pubkey):
        """May agents at this hearth reply to this sender? Blocked: never. If an allowlist exists: only those."""
        if self.is_blocked(pubkey):
            return False
        return not self.lists["allowed"] or pubkey in self.lists["allowed"]

    async def open(self):
        self.chimneys = nostr.Chimneys(self.portkey.chimneys)
        return await self.chimneys.connect()

    async def close(self):
        if self.chimneys:
            await self.chimneys.close()
        self.db.close()

    # ---------- reading events ----------

    def _topic_of(self, ev):
        for t in ev.get("tags", []):
            if len(t) >= 2 and t[0] == "t" and str(t[1]).startswith(self.portkey.topic_prefix):
                return t[1][len(self.portkey.topic_prefix):]
        return "?"

    @staticmethod
    def thread_of(ev):
        """(root, reply_to) from NIP-10 'e' tags: marked tags first, positional as a fallback."""
        es = [t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e"]
        root = next((t[1] for t in es if len(t) >= 4 and t[3] == "root"), None)
        reply = next((t[1] for t in es if len(t) >= 4 and t[3] == "reply"), None)
        if root or reply:
            return root or reply, reply or root
        if es:  # old positional style: first is the root, last is the parent
            return es[0][1], es[-1][1]
        return None, None

    def record(self, ev, status="received"):
        """Store an event; returns it as a plain message dict. If it's already stored, only fill in
        fields older history files lacked (never overwrite a delivery status)."""
        root, reply_to = self.thread_of(ev)
        msg = {
            "id": ev["id"], "pubkey": ev["pubkey"],
            "name": str(nostr.tag_value(ev, "name") or "anon")[:40],
            "topic": self._topic_of(ev), "content": ev["content"],
            "created_at": ev["created_at"], "reply_to": reply_to,
            "mine": int(ev["pubkey"] == self.pubkey), "root": root,
            "status": status, "raw": json.dumps(ev, ensure_ascii=False),
        }
        self.db.execute(f"INSERT INTO messages ({','.join(COLUMNS)}) "
                        f"VALUES ({','.join(':' + c for c in COLUMNS)}) "
                        "ON CONFLICT(id) DO UPDATE SET raw=COALESCE(raw, excluded.raw), "
                        "root=COALESCE(root, excluded.root), reply_to=COALESCE(reply_to, excluded.reply_to), "
                        "status=COALESCE(status, excluded.status)", msg)
        self.db.commit()
        return {k: v for k, v in msg.items() if k != "raw"}

    def is_mine(self, event_id):
        if not event_id:
            return False
        return self.db.execute("SELECT 1 FROM messages WHERE id=? AND mine=1", (event_id,)).fetchone() is not None

    def _set_status(self, event_id, status):
        self.db.execute("UPDATE messages SET status=? WHERE id=?", (status, event_id))
        self.db.commit()

    # ---------- posting ----------

    def _thread_tags(self, reply_to):
        row = self.db.execute("SELECT root FROM messages WHERE id=?", (reply_to,)).fetchone()
        root = (row[0] if row and row[0] else None) or reply_to
        return [["e", root, "", "root"], ["e", reply_to, "", "reply"]]

    async def _send(self, ev):
        """Publish a signed event and update its outbox status. Returns (status, results)."""
        results = await self.chimneys.publish(ev)
        if any(ok for ok, _ in results.values()):
            status = "sent"
        elif all(note not in ("no reply", "not connected", "connection closed") for _, note in results.values()):
            status = "failed"       # every chimney answered, and every one refused
        else:
            status = "unconfirmed"  # maybe delivered, maybe not: safe to retry, the event id is the same
        self._set_status(ev["id"], status)
        return status, results

    async def post(self, topic, content, reply_to=None):
        """Sign and send a message. The signed event is saved first, so a failed send can be retried.
        Returns (message, {chimney: (accepted, note)}). Raises ConnectionError if no chimney accepted it."""
        tags = [["t", self.portkey.topic_tag(topic)], ["name", self.name],
                ["client", "floo-network"], ["floo", PROTOCOL_VERSION]]
        if reply_to:
            tags += self._thread_tags(reply_to)
        ev = nostr.make_event(self.private_key, self.portkey.kind, content, tags)
        msg = self.record(ev, status="pending")
        status, results = await self._send(ev)
        msg["status"] = status
        if status != "sent":
            raise ConnectionError(f"message saved but not delivered ({status}); "
                                  f"run 'floo retry {self.name}' later. Chimneys said: {results}")
        return msg, results

    def outbox(self):
        """My messages that haven't been confirmed delivered."""
        rows = self.db.execute("SELECT raw FROM messages WHERE mine=1 AND status IN "
                               "('pending','unconfirmed','failed') ORDER BY created_at").fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    async def retry(self):
        """Resend undelivered messages (same signed events, so chimneys can't double-post them)."""
        return [(ev, *(await self._send(ev))) for ev in self.outbox()]

    # ---------- fetching ----------

    def _filter(self, topics, since=None, limit=None):
        flt = {"kinds": [self.portkey.kind], "#t": [self.portkey.topic_tag(t) for t in topics]}
        if since:
            flt["since"] = int(since)
        if limit:
            flt["limit"] = limit
        return flt

    async def fetch(self, topics, since=None, limit=50):
        """Fetch stored messages from the chimneys, oldest first."""
        sub_id, queue = await self.chimneys.subscribe(self._filter(topics, since, limit))
        await self.chimneys.wait_stored(sub_id)
        await self.chimneys.unsubscribe(sub_id)
        msgs = []
        while not queue.empty():
            msgs.append(self.record(queue.get_nowait()))
        return sorted(msgs, key=lambda m: m["created_at"])

    async def follow(self, topics, since, limit=30, caught_up=None):
        """Catch up on recent messages, then keep following live ones, on a single subscription
        so nothing posted in between is missed. Yields (msg, is_new); is_new is False for
        messages that were already there before we started. Sets the `caught_up` event (if given)
        once the stored messages are in."""
        started = int(time.time())
        sub_id, queue = await self.chimneys.subscribe(self._filter(topics, since, limit))
        try:
            await self.chimneys.wait_stored(sub_id)
            backlog = []
            while not queue.empty():
                backlog.append(queue.get_nowait())
            for ev in sorted(backlog, key=lambda e: e["created_at"]):
                # anything posted after we started counts as new, even if it came in with the backlog
                yield self.record(ev), ev["created_at"] >= started
            if caught_up:
                caught_up.set()
            while True:
                # arrived live: new, unless it's stamped well before we started (replayed old traffic)
                ev = await queue.get()
                yield self.record(ev), ev["created_at"] >= started - LIVE_SKEW_SECONDS
        finally:
            await self.chimneys.unsubscribe(sub_id)

    # ---------- local history ----------

    def _rows(self, where, args, limit):
        cols = [c for c in COLUMNS if c != "raw"]
        rows = self.db.execute(f"SELECT {','.join(cols)} FROM messages WHERE {where} "
                               "ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(zip(cols, r)) for r in reversed(rows)]

    def history(self, topic, limit=20):
        return self._rows("topic=?", (normalize_topic(topic),), limit)

    def search(self, text, topic=None, limit=50):
        """Case-insensitive search of everything this hearth has seen."""
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if topic:
            return self._rows("content LIKE ? ESCAPE '\\' AND topic=?", (like, normalize_topic(topic)), limit)
        return self._rows("content LIKE ? ESCAPE '\\'", (like,), limit)

    def export(self, topic=None):
        """Every stored signed event (oldest first), for backup or analysis."""
        sql = "SELECT raw FROM messages WHERE raw IS NOT NULL"
        args = ()
        if topic:
            sql, args = sql + " AND topic=?", (normalize_topic(topic),)
        return [json.loads(r[0]) for r in self.db.execute(sql + " ORDER BY created_at", args)]
