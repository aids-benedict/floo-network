"""Hearth features: reply threads, outbox and retry, search, export, history upgrades, agent budget controls."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from floo import agent as agent_mod
from floo import hearth as hearth_mod
from floo import nostr

FIXTURE = Path(__file__).parent / "fixtures" / "event_v1.json"


class FakeChimneys:
    def __init__(self, answers):
        self.answers = answers  # list of {url: (ok, note)} returned by successive publishes
        self.published = []

    async def publish(self, ev):
        self.published.append(ev)
        return self.answers.pop(0) if self.answers else {"wss://a": (True, "")}


@pytest.fixture
def hearth(tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path)
    hearth_mod.create_hearth("me", backend="mock")
    h = hearth_mod.Hearth("me")
    yield h
    h.db.close()


def incoming(h, text, reply_to=None, root=None):
    tags = [["t", h.portkey.topic_tag("test")], ["name", "other"]]
    if root:
        tags.append(["e", root, "", "root"])
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    return h.record(nostr.make_event(nostr.generate_private_key(), h.portkey.kind, text, tags))


def test_replies_carry_root_and_parent(hearth):
    hearth.chimneys = FakeChimneys([])
    q = incoming(hearth, "question")
    a1 = incoming(hearth, "first answer", reply_to=q["id"], root=q["id"])
    mine, _ = asyncio.run(hearth.post("test", "reply to the answer", reply_to=a1["id"]))
    assert mine["root"] == q["id"] and mine["reply_to"] == a1["id"]
    tags = hearth.chimneys.published[0]["tags"]
    assert ["e", q["id"], "", "root"] in tags and ["e", a1["id"], "", "reply"] in tags


def test_thread_of_positional_fallback():
    ev = {"tags": [["e", "root-id"], ["e", "parent-id"]]}
    assert hearth_mod.Hearth.thread_of(ev) == ("root-id", "parent-id")


def test_outbox_and_retry(hearth):
    hearth.chimneys = FakeChimneys([{"wss://a": (False, "no reply")}, {"wss://a": (True, "")}])
    with pytest.raises(ConnectionError):
        asyncio.run(hearth.post("test", "lost in the floo"))
    assert [e["content"] for e in hearth.outbox()] == ["lost in the floo"]
    results = asyncio.run(hearth.retry())
    assert results[0][1] == "sent"
    assert hearth.outbox() == []
    first, second = hearth.chimneys.published
    assert first["id"] == second["id"], "retry must resend the same signed event"


def test_all_refused_is_failed(hearth):
    hearth.chimneys = FakeChimneys([{"wss://a": (False, "blocked: spam"), "wss://b": (False, "rate-limited")}])
    with pytest.raises(ConnectionError, match="failed"):
        asyncio.run(hearth.post("test", "nope"))


def test_search_is_literal(hearth):
    incoming(hearth, "100% sure about snake_case")
    incoming(hearth, "1000 sure about snakeXcase")
    assert [m["content"] for m in hearth.search("100%")] == ["100% sure about snake_case"]
    assert [m["content"] for m in hearth.search("snake_case")] == ["100% sure about snake_case"]
    assert len(hearth.search("SURE")) == 2  # case-insensitive


def test_export_round_trips_signed_events(hearth):
    incoming(hearth, "keep me ✨")
    evs = hearth.export()
    assert len(evs) == 1 and nostr.verify_event(evs[0])


def test_old_history_file_is_upgraded(tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path)
    hearth_mod.create_hearth("old", backend="mock")
    db = sqlite3.connect(tmp_path / "old" / "history.db")
    db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, pubkey TEXT, name TEXT, topic TEXT, "
               "content TEXT, created_at INTEGER, reply_to TEXT, mine INTEGER)")
    ev = nostr.make_event(nostr.generate_private_key(), 4141, "from v0", [["t", "floo-test"]])
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
               (ev["id"], ev["pubkey"], "x", "test", "from v0", ev["created_at"], None, 0))
    db.commit()
    db.close()
    h = hearth_mod.Hearth("old")
    assert h.export() == []      # old rows had no signed copy...
    h.record(ev)                 # ...until the message is seen again
    assert h.export() == [ev]
    h.db.close()


def test_protocol_fixture_verifies():
    ev = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert nostr.well_formed(ev) and nostr.verify_event(ev)
    assert ["floo", "1"] in ev["tags"] and ev["kind"] == 4141
    reply = json.loads((FIXTURE.parent / "reply_v1.json").read_text(encoding="utf-8"))
    assert nostr.verify_event(reply)
    assert hearth_mod.Hearth.thread_of(reply) == (ev["id"], ev["id"])


# ---------- agent budget controls ----------

def make_agent(hearth, **cfg):
    hearth.agent_cfg = {**hearth.agent_cfg, **cfg}
    return agent_mod.Agent(hearth, "test", minutes=1, say=lambda *_: None)


def test_mentions_mode(hearth):
    hearth.chimneys = FakeChimneys([])
    ag = make_agent(hearth, respond_to="mentions")
    mine, _ = asyncio.run(hearth.post("test", "my question"))
    assert not ag.wants(incoming(hearth, "unrelated chatter"))
    assert ag.wants(incoming(hearth, "hey @me what do you think?"))
    assert ag.wants(incoming(hearth, "answering you", reply_to=mine["id"], root=mine["id"]))


def test_per_sender_limit(hearth):
    ag = make_agent(hearth, max_replies_per_sender=2)
    msg = incoming(hearth, "hi")
    ag.replies_to[msg["pubkey"]] = 2
    assert not ag.wants(msg)
    assert ag.wants(incoming(hearth, "someone else"))
