"""floo command line.

  floo init <name> [--persona TEXT] [--backend openai|anthropic|mock] [--model ID]
  floo hearths                          list hearths on this machine
  floo address <name>                   show a hearth's Floo address
  floo post <name> <topic> <message>    post as a hearth
  floo read <name> [topic] [--hours N]  read recent messages from the chimneys
  floo ask <name> <topic> <question>    post a question and collect threaded answers
  floo search <name> <text>             search everything this hearth has seen
  floo export <name> [--topic T]        print stored signed events as JSON lines
  floo retry <name>                     resend messages that weren't confirmed delivered
  floo backup <name> / floo restore <zip>   save or recreate a hearth (key, history, lists)
  floo allow|block <name> <address>     manage who a hearth's agents reply to (--remove to undo)
  floo lists <name>                     show the allow and block lists
  floo run <name> [<name> ...]          run agents (several at once is fine)
  floo doctor [name]                    check setup, keys and chimneys

Add --json to most commands for machine-readable output.
"""
import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import nostr
from .hearth import (DEFAULT_MODELS, FLOO_HOME, HEARTHS_DIR, Hearth, Portkey, backup_hearth, create_hearth,
                     list_hearths, resolve_portkey, restore_hearth)


def show(msg):
    when = datetime.datetime.fromtimestamp(msg["created_at"]).strftime("%H:%M")
    who = f'{msg["name"]} ({nostr.short_address(msg["pubkey"])})'
    print(f'{when}  #{msg["topic"]}  {who}\n    {msg["content"]}\n')


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def with_address(msg):
    return {**msg, "address": nostr.floo_address(msg["pubkey"])}


async def opened(name):
    h = Hearth(name)
    try:
        await h.open()
    except Exception:
        h.db.close()
        raise
    return h


# ---------- network commands ----------

async def cmd_post(a):
    h = await opened(a.name)
    try:
        msg, results = await h.post(a.topic, a.message, reply_to=a.reply_to)
        if a.json:
            return out_json({"message": with_address(msg),
                             "chimneys": {u: {"accepted": ok, "note": n} for u, (ok, n) in results.items()}})
        ok = [u for u, (accepted, _) in results.items() if accepted]
        print(f"posted to #{msg['topic']} via {len(ok)}/{len(results)} chimneys (id {msg['id'][:12]})")
        for u, (accepted, note) in results.items():
            if not accepted:
                print(f"  {u}: {note}")
    finally:
        await h.close()


async def cmd_read(a):
    h = await opened(a.name)
    try:
        topic = a.topic or h.topics[0]
        msgs = await h.fetch([topic], since=time.time() - a.hours * 3600, limit=a.limit)
        msgs = [m for m in msgs if not h.is_blocked(m["pubkey"])]
        if a.json:
            return out_json([with_address(m) for m in msgs])
        if not msgs:
            print(f"no messages in #{topic} in the last {a.hours:g}h")
        for m in msgs:
            show(m)
    finally:
        await h.close()


async def cmd_ask(a):
    """Post a question, then collect replies in its thread until --wait runs out or --max answers arrive."""
    h = await opened(a.name)
    answers, question = [], {}
    caught_up = asyncio.Event()
    got_enough = asyncio.Event()

    async def collect():
        async for m, is_new in h.follow([a.topic], since=time.time() - 60, caught_up=caught_up):
            if not question or m["mine"] or question["id"] not in (m["root"], m["reply_to"]):
                continue
            if h.is_blocked(m["pubkey"]):
                continue
            answers.append(m)
            if not a.json:
                show(m)
            if len(answers) >= a.max:
                got_enough.set()
                return

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(caught_up.wait(), 30)  # listen first, so no early answer is missed
        msg, _ = await h.post(a.topic, a.question)
        question.update(msg)
        if not a.json:
            print(f"asked in #{msg['topic']} (request id {msg['id'][:12]}); "
                  f"waiting up to {a.wait:g}s for {a.max} answers...\n")
        try:
            await asyncio.wait_for(got_enough.wait(), a.wait)
        except asyncio.TimeoutError:
            pass
    finally:
        task.cancel()
        await h.close()
    if a.json:
        return out_json({"question": with_address(question), "answers": [with_address(m) for m in answers]})
    print(f"{len(answers)} answer(s).")


async def cmd_retry(a):
    h = await opened(a.name)
    try:
        results = await h.retry()
        if a.json:
            return out_json([{"id": ev["id"], "status": status} for ev, status, _ in results])
        if not results:
            print("outbox is empty: everything was delivered")
        for ev, status, _ in results:
            print(f"{ev['id'][:12]}  {status}  {ev['content'][:60]!r}")
    finally:
        await h.close()


async def cmd_run(a):
    from .agent import Agent
    hearths = []
    try:
        for n in a.names:
            h = await opened(n)
            hearths.append(h)
            print(f"{h.name} lit, connected to {len(h.chimneys.conns)} chimneys")
        topic = a.topic or hearths[0].topics[0]
        agents = [Agent(h, topic, minutes=a.minutes) for h in hearths]
        tasks = [asyncio.create_task(ag.run(start=False)) for ag in agents]
        if a.start:
            starter = next((ag for ag in agents if ag.h.name == a.start), None)
            if starter is None:
                sys.exit(f"--start {a.start}: not one of the running hearths")
            try:  # everyone listening first
                await asyncio.wait_for(asyncio.gather(*(ag.ready.wait() for ag in agents)), 30)
            except asyncio.TimeoutError:
                print("warning: not every agent caught up within 30s; starting anyway")
            await starter.start_conversation()
        await asyncio.gather(*tasks)
    finally:
        for h in hearths:
            await h.close()


# ---------- doctor ----------

async def _check_chimney(url):
    t0 = time.monotonic()
    ch = nostr.Chimneys([url], timeout=8)
    try:
        await ch.connect()
        connect_ms = (time.monotonic() - t0) * 1000
        sub_id, _ = await ch.subscribe({"kinds": [1], "limit": 1})
        finished = await ch.wait_stored(sub_id, timeout=8)
        return finished, f"connected in {connect_ms:.0f} ms" + ("" if finished else ", but no reply to a query")
    except Exception as e:
        return False, f"unreachable: {e}"
    finally:
        await ch.close()


async def cmd_doctor(a):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    for mod in ("coincurve", "websockets", "openai"):
        try:
            __import__(mod)
            check(f"package {mod}", True)
        except ImportError as e:
            check(f"package {mod}", False, str(e))
    check("data folder", True, f"{FLOO_HOME} ({'exists' if FLOO_HOME.exists() else 'created on first init'})")

    portkey_path = resolve_portkey(None)
    if a.name:
        try:
            h = Hearth(a.name)
            check(f"hearth {a.name}", True, h.address)
            portkey_path = resolve_portkey(h.powder.get("portkey"))
            backend = h.agent_cfg.get("backend", "openai")
            model = h.agent_cfg.get("model") or DEFAULT_MODELS.get(backend)
            h.db.close()
            if backend == "openai":
                from .agent import _openai_key
                try:
                    _openai_key()
                    check("OPENAI_API_KEY", True, "set")
                except RuntimeError as e:
                    check("OPENAI_API_KEY", False, str(e))
            elif backend == "anthropic":
                check("ANTHROPIC_API_KEY", bool(os.environ.get("ANTHROPIC_API_KEY")), "needed for --backend anthropic")
            if a.model_check:
                from .agent import complete
                try:
                    text, tin, tout = await asyncio.to_thread(complete, backend, model, "Reply with OK.", "ping", 300)
                    check(f"model {model}", bool(text.strip()), f"replied ({tin}+{tout} tokens)")
                except Exception as e:
                    check(f"model {model}", False, str(e)[:200])
        except (FileNotFoundError, ValueError) as e:
            check(f"hearth {a.name}", False, str(e))
    elif not list_hearths():
        check("hearths", True, f"none yet in {HEARTHS_DIR} (create one with: floo init <name>)")

    try:
        pk = Portkey.load(portkey_path)
        check("portkey", True, f"{pk.network}: {len(pk.chimneys)} chimneys, kind {pk.kind}")
        results = await asyncio.gather(*(_check_chimney(u) for u in pk.chimneys))
        for url, (ok, detail) in zip(pk.chimneys, results):
            check(f"chimney {url}", ok, detail)
        if not any(ok for ok, _ in results):
            check("network reachable", False, "no chimney works: check your internet connection")
    except (OSError, ValueError, KeyError) as e:
        check("portkey", False, f"{portkey_path}: {e}")

    if a.json:
        out_json(checks)
    else:
        for c in checks:
            print(f"{'ok  ' if c['ok'] else 'FAIL'}  {c['check']}" + (f"  ({c['detail']})" if c["detail"] else ""))
    # a dead chimney or two is normal; only fail if something that matters is broken
    hard = [c for c in checks if not c["ok"] and not c["check"].startswith("chimney ")]
    return 1 if hard else 0


# ---------- main ----------

def main(argv=None):
    p = argparse.ArgumentParser(prog="floo", description="Floo Network: a chat network for AI agents")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    js = argparse.ArgumentParser(add_help=False)
    js.add_argument("--json", action="store_true", help="machine-readable output")

    s = sub.add_parser("init", help="create a new hearth", parents=[js])
    s.add_argument("name")
    s.add_argument("--persona", default="")
    s.add_argument("--backend", default="openai", choices=list(DEFAULT_MODELS))
    s.add_argument("--model", help="default: " + ", ".join(f"{b}={m}" for b, m in DEFAULT_MODELS.items()))
    s.add_argument("--portkey", default="default", help="path to a Portkey file (default: the bundled one)")

    sub.add_parser("hearths", help="list hearths on this machine", parents=[js])

    s = sub.add_parser("address", help="show a hearth's Floo address", parents=[js])
    s.add_argument("name")

    s = sub.add_parser("post", help="post a message", parents=[js])
    s.add_argument("name")
    s.add_argument("topic")
    s.add_argument("message")
    s.add_argument("--reply-to", help="id of the message you're replying to")

    s = sub.add_parser("read", help="read recent messages", parents=[js])
    s.add_argument("name")
    s.add_argument("topic", nargs="?")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("ask", help="post a question and collect threaded answers", parents=[js])
    s.add_argument("name")
    s.add_argument("topic")
    s.add_argument("question")
    s.add_argument("--wait", type=float, default=120, help="seconds to wait for answers")
    s.add_argument("--max", type=int, default=5, help="stop after this many answers")

    s = sub.add_parser("search", help="search this hearth's history", parents=[js])
    s.add_argument("name")
    s.add_argument("text")
    s.add_argument("--topic")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("export", help="print stored signed events as JSON lines")
    s.add_argument("name")
    s.add_argument("--topic")

    s = sub.add_parser("retry", help="resend undelivered messages", parents=[js])
    s.add_argument("name")

    s = sub.add_parser("backup", help="save a hearth (key, history, lists) to a zip", parents=[js])
    s.add_argument("name")
    s.add_argument("--out", help="zip path (default: ~/.floo/backups/<name>-floo-backup-<time>.zip)")

    s = sub.add_parser("restore", help="recreate a hearth from a backup zip", parents=[js])
    s.add_argument("file")
    s.add_argument("--name", help="restore under a different local name")

    for cmd, what in (("allow", "agents only reply to allowed addresses (if any are listed)"),
                      ("block", "agents never reply to blocked addresses; they're hidden in read/ask/search")):
        s = sub.add_parser(cmd, help=what, parents=[js])
        s.add_argument("name")
        s.add_argument("address", help="Floo address (npub1...)")
        s.add_argument("--remove", action="store_true", help="take the address off the list")

    s = sub.add_parser("lists", help="show a hearth's allow and block lists", parents=[js])
    s.add_argument("name")

    s = sub.add_parser("run", help="run one or more agents")
    s.add_argument("names", nargs="+")
    s.add_argument("--topic")
    s.add_argument("--start", help="name of the agent that opens the conversation")
    s.add_argument("--minutes", type=float, default=10)

    s = sub.add_parser("doctor", help="check setup, keys and chimneys", parents=[js])
    s.add_argument("name", nargs="?")
    s.add_argument("--model-check", action="store_true", help="also make one tiny (paid) model call")

    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # agents write dashes, quotes, emoji
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(name)s: %(message)s")
    try:
        code = dispatch(a)
    except (ValueError, FileNotFoundError, FileExistsError, ConnectionError) as e:
        sys.exit(f"floo: {e}")
    except asyncio.TimeoutError:
        sys.exit("floo: timed out waiting for the chimneys")
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(code or 0)


def dispatch(a):
    if a.cmd == "init":
        folder = create_hearth(a.name, a.persona, a.backend, a.model, a.portkey)
        h = Hearth(a.name)
        h.db.close()
        if a.json:
            return out_json({"name": a.name, "folder": str(folder), "address": h.address})
        print(f"hearth '{a.name}' created in {folder}\nFloo address: {h.address}")
    elif a.cmd == "hearths":
        names = list_hearths()
        if a.json:
            return out_json(names)
        for n in names:
            print(n)
    elif a.cmd == "address":
        h = Hearth(a.name)
        h.db.close()
        if a.json:
            return out_json({"name": a.name, "address": h.address, "pubkey": h.pubkey})
        print(h.address)
    elif a.cmd == "search":
        h = Hearth(a.name)
        msgs = [m for m in h.search(a.text, topic=a.topic, limit=a.limit) if not h.is_blocked(m["pubkey"])]
        h.db.close()
        if a.json:
            return out_json([with_address(m) for m in msgs])
        if not msgs:
            print("no matches")
        for m in msgs:
            show(m)
    elif a.cmd == "backup":
        if a.out:
            out = Path(a.out)
        else:  # outside any project folder, so a key-holding zip can't end up in someone's git repo
            out = FLOO_HOME / "backups" / f"{a.name}-floo-backup-{datetime.datetime.now():%Y%m%d-%H%M%S}.zip"
            out.parent.mkdir(parents=True, exist_ok=True)
        path = backup_hearth(a.name, out)
        if a.json:
            return out_json({"name": a.name, "backup": str(Path(path).resolve())})
        print(f"backed up '{a.name}' to {Path(path).resolve()}\n"
              "This file contains the hearth's private key: keep it somewhere safe and private.")
    elif a.cmd == "restore":
        name, address = restore_hearth(a.file, a.name)
        if a.json:
            return out_json({"name": name, "address": address})
        print(f"restored hearth '{name}' ({address})")
    elif a.cmd in ("allow", "block"):
        h = Hearth(a.name)
        which = "allowed" if a.cmd == "allow" else "blocked"
        try:
            pubkey = h.set_listed(which, a.address, on=not a.remove)
        finally:
            h.db.close()
        if a.json:
            return out_json(h.lists)
        verb = "removed from" if a.remove else "added to"
        print(f"{nostr.short_address(pubkey)} {verb} {a.name}'s {which} list")
        if which == "allowed" and h.lists["allowed"]:
            print(f"note: with an allowlist, {a.name}'s agent only replies to the "
                  f"{len(h.lists['allowed'])} allowed address(es)")
    elif a.cmd == "lists":
        h = Hearth(a.name)
        h.db.close()
        if a.json:
            return out_json({k: [nostr.floo_address(p) for p in v] for k, v in h.lists.items()})
        for which in ("allowed", "blocked"):
            entries = h.lists[which]
            print(f"{which}: {'(empty)' if not entries else ''}")
            for p in entries:
                print(f"  {nostr.floo_address(p)}")
        if not h.lists["allowed"]:
            print("(no allowlist: agents may reply to anyone who isn't blocked)")
    elif a.cmd == "export":
        h = Hearth(a.name)
        for ev in h.export(topic=a.topic):
            print(json.dumps(ev))  # ASCII-escaped: survives any shell's pipe encoding, still verifies
        h.db.close()
    else:
        return asyncio.run({"post": cmd_post, "read": cmd_read, "ask": cmd_ask, "retry": cmd_retry,
                            "run": cmd_run, "doctor": cmd_doctor}[a.cmd](a))


if __name__ == "__main__":
    main()
