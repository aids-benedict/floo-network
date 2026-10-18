"""A real chimney on a random local port, exercised with Floo's own relay client."""
import asyncio
import json
import urllib.request

import pytest
import websockets

from floo import chimney as chimney_mod
from floo import nostr

KIND = 4141
TAGS = [["t", "floo-test"], ["name", "tester"]]


def run(coro):
    return asyncio.run(coro)


async def start(tmp_path, **kw):
    ch = chimney_mod.Chimney(tmp_path / "events.db", [KIND], **kw)
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(ch.serve("127.0.0.1", 0, ready=ready))
    port = await ready
    return ch, task, f"ws://127.0.0.1:{port}"


async def stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def ev(text="hello", sk=None, kind=KIND, tags=TAGS, created_at=None):
    return nostr.make_event(sk or nostr.generate_private_key(), kind, text, tags, created_at=created_at)


def test_publish_store_and_query(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path)
        try:
            async with nostr.Chimneys([url]) as c:
                e = ev("stored and served")
                assert (await c.publish(e))[url][0] is True
                assert (await c.publish(e))[url] == (True, "duplicate: already have this event")
                sub, q = await c.subscribe({"kinds": [KIND], "#t": ["floo-test"]})
                assert await c.wait_stored(sub)
                assert q.get_nowait()["content"] == "stored and served"
        finally:
            await stop(task)
    run(go())


def test_live_delivery_to_other_subscribers(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path)
        try:
            async with nostr.Chimneys([url]) as listener, nostr.Chimneys([url]) as poster:
                sub, q = await listener.subscribe({"kinds": [KIND], "#t": ["floo-test"]})
                await listener.wait_stored(sub)
                await poster.publish(ev("live!"))
                await poster.publish(ev("other topic", tags=[["t", "floo-elsewhere"]]))
                got = await asyncio.wait_for(q.get(), 5)
                assert got["content"] == "live!"
                await asyncio.sleep(0.2)
                assert q.empty(), "events outside the filter must not be delivered"
        finally:
            await stop(task)
    run(go())


def test_rejects_bad_signature_wrong_kind_and_junk(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path)
        try:
            async with websockets.connect(url) as ws:
                forged = ev("forged")
                forged["content"] = "changed after signing"
                await ws.send(json.dumps(["EVENT", forged]))
                assert json.loads(await ws.recv())[2:] == [False, "invalid: bad id or signature"]
                await ws.send(json.dumps(["EVENT", ev("kind 1", kind=1)]))
                reply = json.loads(await ws.recv())
                assert reply[2] is False and reply[3].startswith("blocked:")
                await ws.send(json.dumps(["EVENT", {"id": "x"}]))
                assert json.loads(await ws.recv())[2:] == [False, "invalid: malformed event"]
                await ws.send("not json")
                assert json.loads(await ws.recv())[0] == "NOTICE"
                await ws.send(json.dumps(["REQ", "s", {"kinds": "nope"}]))
                assert json.loads(await ws.recv())[:1] == ["CLOSED"]
            assert ch.store.count() == 0
        finally:
            await stop(task)
    run(go())


def test_rate_limit_per_author(tmp_path, monkeypatch):
    monkeypatch.setattr(chimney_mod, "EVENTS_PER_MINUTE", 3)

    async def go():
        ch, task, url = await start(tmp_path)
        try:
            sk = nostr.generate_private_key()
            async with nostr.Chimneys([url]) as c:
                results = [(await c.publish(ev(f"msg {i}", sk=sk)))[url] for i in range(5)]
            assert [ok for ok, _ in results] == [True, True, True, False, False]
            assert results[-1][1].startswith("rate-limited")
        finally:
            await stop(task)
    run(go())


def test_messages_survive_restart(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path)
        async with nostr.Chimneys([url]) as c:
            await c.publish(ev("remember me"))
        await stop(task)
        ch.store.db.close()
        ch, task, url = await start(tmp_path)
        try:
            async with nostr.Chimneys([url]) as c:
                sub, q = await c.subscribe({"kinds": [KIND]})
                await c.wait_stored(sub)
                assert q.get_nowait()["content"] == "remember me"
        finally:
            await stop(task)
    run(go())


def test_filters_and_limits(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path)
        try:
            sk = nostr.generate_private_key()
            pk = nostr.public_key(sk)
            async with nostr.Chimneys([url]) as c:
                for i in range(4):
                    await c.publish(ev(f"m{i}", sk=sk, created_at=1_700_000_000 + i))
                await c.publish(ev("someone else", created_at=1_700_000_010))
                checks = {
                    "author": ({"authors": [pk]}, 4),
                    "limit": ({"kinds": [KIND], "limit": 2}, 2),
                    "since/until": ({"since": 1_700_000_001, "until": 1_700_000_002}, 2),
                    "nobody": ({"authors": []}, 0),
                }
                for name, (flt, expected) in checks.items():
                    sub, q = await c.subscribe(flt)
                    await c.wait_stored(sub)
                    assert q.qsize() == expected, name
        finally:
            await stop(task)
    run(go())


def test_info_document_and_browser_page(tmp_path):
    async def go():
        ch, task, url = await start(tmp_path, name="Test chimney")
        http = url.replace("ws://", "http://")
        try:
            def fetch(accept):
                req = urllib.request.Request(http, headers={"Accept": accept})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.headers.get("Content-Type"), r.read().decode()
            ctype, body = await asyncio.to_thread(fetch, "application/nostr+json")
            info = json.loads(body)
            assert ctype == "application/nostr+json" and info["name"] == "Test chimney" and 1 in info["supported_nips"]
            ctype, body = await asyncio.to_thread(fetch, "text/html")
            assert "Floo Network chimney" in body
        finally:
            await stop(task)
    run(go())


@pytest.mark.parametrize("flt,ok", [({"kinds": [1], "#t": ["a"]}, True), ({"ids": "x"}, False),
                                    ({"limit": "5"}, False), ({"search": "ignored"}, True)])
def test_clean_filter(flt, ok):
    assert (chimney_mod.clean_filter(flt) is not None) is ok
