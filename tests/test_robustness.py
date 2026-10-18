"""Offline tests for the phase-1 fixes: hard caps, relay reconnect, per-subscription dedupe,
incoming-event checks, hearth names and topics, backend default models."""
import asyncio
import json
import time

import pytest

from floo import agent as agent_mod
from floo import hearth as hearth_mod
from floo import nostr

KIND = 4141
TAG = [["t", "floo-test"], ["name", "other"]]
FILTER = {"kinds": [KIND], "#t": ["floo-test"]}


# ---------- caps ----------

class FakeHearth:
    def __init__(self, folder, **cfg):
        self.name, self.pubkey, self.folder = "me", "00" * 32, folder
        self.agent_cfg = {"backend": "mock", "min_seconds_between_posts": 0, **cfg}
        self.sent = []

    def history(self, topic, limit=15):
        return []

    def is_blocked(self, pubkey):
        return False

    async def post(self, topic, text, reply_to=None):
        await asyncio.sleep(0.01)
        self.sent.append(text)
        return {"content": text}, {}


def test_caps_hold_under_concurrent_turns(tmp_path):
    h = FakeHearth(tmp_path, max_llm_calls=3, max_posts=2)
    ag = agent_mod.Agent(h, "test", minutes=1, say=lambda *_: None)

    async def go():
        await asyncio.gather(ag.start_conversation(), *(ag._turn_once("reply") for _ in range(10)))
    asyncio.run(go())
    assert ag.posts == len(h.sent) == 2
    assert ag.calls <= 3


def test_deadline_rechecked_after_post_gap(tmp_path):
    h = FakeHearth(tmp_path, min_seconds_between_posts=0.3)
    ag = agent_mod.Agent(h, "test", minutes=1, say=lambda *_: None)
    ag.last_post = time.monotonic()           # a post just went out, so the next one must wait
    ag.deadline = time.monotonic() + 0.1      # ...and the deadline passes during that wait
    asyncio.run(ag._post("too late"))
    assert h.sent == []


# ---------- relay pool ----------

class FakeWS:
    """One websocket connection: answers REQs with the given events, then drops if told to."""

    def __init__(self, events, drop_after):
        self.events, self.drop_after = events, drop_after
        self.inbox = asyncio.Queue()
        self.reqs = []

    async def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "REQ":
            self.reqs.append(msg[1])
            for ev in self.events:
                self.inbox.put_nowait(json.dumps(["EVENT", msg[1], ev]))
            self.inbox.put_nowait(json.dumps(["EOSE", msg[1]]))
            if self.drop_after:
                self.inbox.put_nowait(None)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise OSError("connection dropped")
        return item


def ev(text, sk=None, created_at=None):
    return nostr.make_event(sk or nostr.generate_private_key(), KIND, text, TAG, created_at=created_at)


def test_reconnects_and_resubscribes(monkeypatch):
    first, second = ev("before the drop"), ev("after reconnect")
    conns = [FakeWS([first], drop_after=True), FakeWS([first, second], drop_after=False)]
    made = []

    async def fake_connect(url, **kw):
        ws = conns[len(made)]
        made.append(ws)
        return ws

    async def go():
        ch = nostr.Chimneys(["wss://fake"], connect=fake_connect)
        await ch.connect()
        _, queue = await ch.subscribe(FILTER)
        got = [await asyncio.wait_for(queue.get(), 5) for _ in range(2)]
        await ch.close()
        return got

    got = asyncio.run(go())
    assert [g["content"] for g in got] == ["before the drop", "after reconnect"]  # no duplicate of the first
    assert len(made) == 2 and conns[1].reqs, "should reconnect and resend the subscription"


def test_dedupe_is_per_subscription():
    e = ev("shared")

    async def fake_connect(url, **kw):
        return FakeWS([e], drop_after=False)

    async def go():
        ch = nostr.Chimneys(["wss://a", "wss://b"], connect=fake_connect)
        await ch.connect()
        _, q1 = await ch.subscribe(FILTER)
        _, q2 = await ch.subscribe(FILTER)
        await asyncio.sleep(0.1)
        sizes = q1.qsize(), q2.qsize()
        await ch.close()
        return sizes

    assert asyncio.run(go()) == (1, 1)  # each subscription sees it once, despite two chimneys


# ---------- incoming event checks ----------

def test_well_formed_rejects_bad_events():
    good = ev("fine")
    assert nostr.well_formed(good)
    assert not nostr.well_formed({**good, "id": "xyz"})
    assert not nostr.well_formed({**good, "created_at": "123"})
    assert not nostr.well_formed({**good, "tags": [["t", 5]]})
    assert not nostr.well_formed({**good, "content": "x" * (nostr.MAX_CONTENT + 1)})
    assert not nostr.well_formed(ev("from the future", created_at=time.time() + 3600))


def test_matches_checks_filter():
    assert nostr.matches(FILTER, ev("in topic"))
    other = nostr.make_event(nostr.generate_private_key(), KIND, "elsewhere", [["t", "floo-other"]])
    assert not nostr.matches(FILTER, other)
    assert not nostr.matches(FILTER, nostr.make_event(nostr.generate_private_key(), 1, "kind 1", TAG))


# ---------- names, topics, defaults ----------

@pytest.mark.parametrize("bad", ["../evil", "Alice", "", "a" * 33, "a/b", "a\\b", ".hidden"])
def test_bad_hearth_names_rejected(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path)
    with pytest.raises(ValueError):
        hearth_mod.create_hearth(bad, backend="mock")


def test_topics_normalized():
    assert hearth_mod.normalize_topic("#Ideas") == "ideas"
    with pytest.raises(ValueError):
        hearth_mod.normalize_topic("bad topic!")


def test_backend_default_models(tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path)
    hearth_mod.create_hearth("ann", backend="anthropic")
    h = hearth_mod.Hearth("ann")
    assert h.agent_cfg["model"] == "claude-haiku-5-5"
    h.db.close()
