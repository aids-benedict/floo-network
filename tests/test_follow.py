"""Hearth.follow must not miss a message posted while it is catching up on history."""
import asyncio
import time

from floo import hearth as hearth_mod
from floo import nostr


class FakeChimneys:
    """Stands in for the relays: serves a backlog, and a 'gap' message arrives mid-catch-up."""

    def __init__(self, backlog, during_catchup, after):
        self.backlog, self.during_catchup, self.after = backlog, during_catchup, after
        self.queue = asyncio.Queue()

    async def subscribe(self, flt):
        for ev in self.backlog:
            self.queue.put_nowait(ev)
        return "sub", self.queue

    async def wait_stored(self, sub_id, timeout=None):
        await asyncio.sleep(0.01)
        for ev in self.during_catchup:  # posted while we were still loading history
            self.queue.put_nowait(ev)

    async def unsubscribe(self, sub_id):
        pass

    def send_live(self):
        for ev in self.after:
            self.queue.put_nowait(ev)


def test_follow_catches_gap_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path)
    hearth_mod.create_hearth("me", backend="mock")
    h = hearth_mod.Hearth("me")
    other = nostr.generate_private_key()
    tag = [["t", h.portkey.topic_tag("test")], ["name", "other"]]
    old = nostr.make_event(other, h.portkey.kind, "old message", tag, created_at=time.time() - 600)
    gap = nostr.make_event(other, h.portkey.kind, "posted during catch-up", tag)
    skewed = nostr.make_event(other, h.portkey.kind, "live, but sender clock is behind", tag,
                              created_at=time.time() - 120)
    replayed = nostr.make_event(other, h.portkey.kind, "old message replayed live", tag,
                                created_at=time.time() - 3600)
    h.chimneys = FakeChimneys([old], [gap], [skewed, replayed])

    async def go():
        caught_up = asyncio.Event()
        seen = []
        gen = h.follow(["test"], since=time.time() - 3600, caught_up=caught_up)
        async for msg, is_new in gen:
            seen.append((msg["content"], is_new))
            if len(seen) == 2:  # backlog done; now a live message arrives
                h.chimneys.send_live()
            if len(seen) == 4:
                break
        await gen.aclose()
        return seen, caught_up.is_set()

    seen, caught = asyncio.run(go())
    h.db.close()
    assert caught
    assert seen == [("old message", False),
                    ("posted during catch-up", True),
                    ("live, but sender clock is behind", True),
                    ("old message replayed live", False)]
