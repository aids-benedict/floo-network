"""Minimal Nostr (NIP-01) client: keys, signed events, and a pool of relay connections (chimneys)."""
import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid

import coincurve
import websockets

log = logging.getLogger("floo.nostr")

# ---------- bech32 (npub / nsec), BIP-173 ----------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc, bits, ret, maxv = 0, 0, [], (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        raise ValueError("invalid padding")
    return ret


def bech32_encode(hrp, hex_str):
    data = _convertbits(bytes.fromhex(hex_str), 8, 5)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def bech32_decode(s):
    s = s.lower()
    hrp, _, rest = s.rpartition("1")
    data = [_CHARSET.index(c) for c in rest]
    if _polymod(_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, pad=False)).hex()


# ---------- keys and events ----------

def generate_private_key():
    return secrets.token_hex(32)


def public_key(private_hex):
    """32-byte x-only public key (hex), as Nostr uses."""
    return coincurve.PrivateKey(bytes.fromhex(private_hex)).public_key_xonly.format().hex()


def floo_address(pubkey_hex):
    """A hearth's Floo address is its npub."""
    return bech32_encode("npub", pubkey_hex)


def short_address(pubkey_hex):
    a = floo_address(pubkey_hex)
    return a[:12] + "..." + a[-4:]


def _event_id(pubkey, created_at, kind, tags, content):
    serial = json.dumps([0, pubkey, created_at, kind, tags, content],
                        separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def make_event(private_hex, kind, content, tags=(), created_at=None):
    pubkey = public_key(private_hex)
    created_at = int(created_at if created_at is not None else time.time())
    tags = [list(t) for t in tags]
    eid = _event_id(pubkey, created_at, kind, tags, content)
    sig = coincurve.PrivateKey(bytes.fromhex(private_hex)).sign_schnorr(bytes.fromhex(eid))
    return {"id": eid, "pubkey": pubkey, "created_at": created_at, "kind": kind,
            "tags": tags, "content": content, "sig": sig.hex()}


def verify_event(ev):
    """True only if the id matches the content and the signature matches the pubkey."""
    try:
        if _event_id(ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]) != ev["id"]:
            return False
        key = coincurve.PublicKeyXOnly(bytes.fromhex(ev["pubkey"]))
        return key.verify(bytes.fromhex(ev["sig"]), bytes.fromhex(ev["id"]))
    except (KeyError, ValueError, TypeError):
        return False


def tag_value(ev, name):
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == name:
            return t[1]
    return None


# ---------- incoming event checks ----------

MAX_CONTENT = 64 * 1024
MAX_TAGS = 100
MAX_FUTURE_SECONDS = 15 * 60
_HEX = set("0123456789abcdef")


def _is_hex(s, n):
    return isinstance(s, str) and len(s) == n and set(s) <= _HEX


def well_formed(ev, now=None):
    """Structural check before we spend time verifying a signature or store anything."""
    if not isinstance(ev, dict):
        return False
    if not (_is_hex(ev.get("id"), 64) and _is_hex(ev.get("pubkey"), 64) and _is_hex(ev.get("sig"), 128)):
        return False
    created, kind = ev.get("created_at"), ev.get("kind")
    if type(created) is not int or type(kind) is not int:
        return False
    if created > (now or time.time()) + MAX_FUTURE_SECONDS:
        return False
    content, tags = ev.get("content"), ev.get("tags")
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        return False
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        return False
    return all(isinstance(t, list) and all(isinstance(x, str) for x in t) for t in tags)


def matches(flt, ev):
    """Does the event fit the subscription filter? Relays are not trusted to have checked."""
    if "kinds" in flt and ev["kind"] not in flt["kinds"]:
        return False
    if "#t" in flt and not any(len(t) >= 2 and t[0] == "t" and t[1] in flt["#t"] for t in ev["tags"]):
        return False
    return True


class _SeenIds:
    """Bounded set of event ids, so long runs don't grow memory forever."""

    def __init__(self, cap=20000):
        self.cap, self._d = cap, {}

    def add(self, key):
        self._d[key] = None
        if len(self._d) > self.cap:
            del self._d[next(iter(self._d))]

    def __contains__(self, key):
        return key in self._d


# ---------- relay pool ----------

class Chimneys:
    """Connections to a set of relays. Reconnects dropped chimneys and resubscribes.
    Each subscription gets its own deduped, verified, bounded queue of events."""

    QUEUE_SIZE = 1000

    def __init__(self, urls, timeout=8, connect=websockets.connect):
        self.urls = list(urls)
        self.timeout = timeout
        self._connect = connect
        self.conns = {}            # url -> live websocket
        self._tasks = []
        self._ok_waiters = {}      # event id -> {url: future}
        self._subs = {}            # sub id -> {"filter", "queue", "eose", "seen"}
        self._closing = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self):
        """Start a connection loop per chimney; return once each has made its first attempt."""
        first_tries = []
        for url in self.urls:
            tried = asyncio.Event()
            first_tries.append(tried)
            self._tasks.append(asyncio.create_task(self._run(url, tried)))
        await asyncio.gather(*(t.wait() for t in first_tries))
        if not self.conns:
            await self.close()
            raise ConnectionError("no chimneys reachable")
        return list(self.conns)

    async def close(self):
        self._closing = True
        for t in self._tasks:
            t.cancel()
        for ws in list(self.conns.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self.conns.clear()

    async def _run(self, url, tried):
        delay, attempts, ever_connected = 1, 0, False
        while not self._closing:
            attempts += 1
            try:
                ws = await self._connect(url, open_timeout=self.timeout, max_size=2**20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # say it loudly once; after that, keep retrying quietly
                (log.warning if attempts == 1 else log.info)("chimney %s unreachable: %s", url, e)
                tried.set()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            self.conns[url] = ws
            tried.set()
            if ever_connected:
                log.warning("chimney %s reconnected", url)
            ever_connected, delay = True, 1
            try:
                for sub_id, state in list(self._subs.items()):
                    await ws.send(json.dumps(["REQ", sub_id, state["filter"]]))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    self._dispatch(url, msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # ConnectionClosed or a broken frame
                if not self._closing:
                    log.warning("chimney %s dropped (%s); reconnecting", url, type(e).__name__)
            finally:
                self.conns.pop(url, None)
                # nothing more will arrive from this connection: count it as done for every subscription
                for state in self._subs.values():
                    state["eose"].add(url)
                for waiters in self._ok_waiters.values():
                    fut = waiters.get(url)
                    if fut and not fut.done():
                        fut.set_result((False, "connection closed"))
            if not self._closing:
                await asyncio.sleep(delay)

    def _dispatch(self, url, msg):
        if not isinstance(msg, list) or not msg:
            return
        kind = msg[0]
        if kind == "OK" and len(msg) >= 3:
            fut = self._ok_waiters.get(msg[1], {}).get(url)
            if fut and not fut.done():
                fut.set_result((bool(msg[2]), msg[3] if len(msg) > 3 else ""))
        elif kind == "EVENT" and len(msg) >= 3:
            state = self._subs.get(msg[1])
            ev = msg[2]
            if state is None or not well_formed(ev) or ev["id"] in state["seen"]:
                return
            if not matches(state["filter"], ev):
                log.info("dropped event from %s that doesn't match the subscription", url)
                return
            if not verify_event(ev):
                log.info("dropped event with bad signature from %s", url)
                return
            state["seen"].add(ev["id"])
            try:
                state["queue"].put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("subscription %s is backed up; dropping an event", msg[1])
        elif kind == "EOSE" and len(msg) >= 2:
            state = self._subs.get(msg[1])
            if state:
                state["eose"].add(url)
        elif kind == "CLOSED" and len(msg) >= 2:
            state = self._subs.get(msg[1])
            if state:
                state["eose"].add(url)
            log.warning("chimney %s closed a subscription: %s", url, msg[2] if len(msg) > 2 else "")
        elif kind == "NOTICE":
            log.info("chimney %s says: %s", url, msg[1:])

    async def publish(self, ev):
        """Send an event to every connected chimney. Returns {url: (accepted, message)}."""
        loop = asyncio.get_running_loop()
        results = {url: (False, "not connected") for url in self.urls}
        waiters = {url: loop.create_future() for url in self.conns}
        self._ok_waiters[ev["id"]] = waiters
        for url, ws in list(self.conns.items()):
            try:
                await ws.send(json.dumps(["EVENT", ev], ensure_ascii=False))
            except Exception as e:
                if not waiters[url].done():
                    waiters[url].set_result((False, str(e)))
        for url, fut in waiters.items():
            try:
                results[url] = await asyncio.wait_for(fut, self.timeout)
            except asyncio.TimeoutError:
                results[url] = (False, "no reply")
        del self._ok_waiters[ev["id"]]
        return results

    async def subscribe(self, flt):
        """Start a subscription. Returns (sub_id, queue of verified, deduped events)."""
        sub_id = uuid.uuid4().hex[:12]
        state = {"filter": flt, "queue": asyncio.Queue(self.QUEUE_SIZE), "eose": set(), "seen": _SeenIds()}
        self._subs[sub_id] = state
        for ws in list(self.conns.values()):
            try:
                await ws.send(json.dumps(["REQ", sub_id, flt]))
            except Exception:
                pass
        return sub_id, state["queue"]

    async def wait_stored(self, sub_id, timeout=None):
        """Wait until every chimney has sent its stored events (EOSE), or isn't connected.
        Returns False if some chimney never finished within the timeout."""
        deadline = time.monotonic() + (timeout or self.timeout)
        state = self._subs[sub_id]
        while time.monotonic() < deadline:
            if all(u in state["eose"] or u not in self.conns for u in self.urls):
                return True
            await asyncio.sleep(0.1)
        slow = [u for u in self.urls if u in self.conns and u not in state["eose"]]
        log.warning("still waiting on stored messages from %s; carrying on", ", ".join(slow))
        return False

    async def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)
        for ws in list(self.conns.values()):
            try:
                await ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass
