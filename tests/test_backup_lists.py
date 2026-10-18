"""Backup/restore keeps identities intact; allow and block lists control who agents answer."""
import json
import zipfile

import pytest

from floo import agent as agent_mod
from floo import hearth as hearth_mod
from floo import nostr


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hearth_mod, "HEARTHS_DIR", tmp_path / "hearths")
    return tmp_path


def make(name="me"):
    hearth_mod.create_hearth(name, backend="mock")
    return hearth_mod.Hearth(name)


def other_msg(h, sk, text="hello?"):
    ev = nostr.make_event(sk, h.portkey.kind, text, [["t", h.portkey.topic_tag("test")], ["name", "other"]])
    return h.record(ev)


# ---------- backup / restore ----------

def test_backup_and_restore_keep_identity_and_history(home):
    h = make("alice")
    other_msg(h, nostr.generate_private_key(), "remember me")
    h.set_listed("blocked", nostr.floo_address(nostr.public_key(nostr.generate_private_key())))
    address, lists = h.address, h.lists
    h.db.close()
    zip_path = hearth_mod.backup_hearth("alice", home / "alice.zip")

    restored, restored_address = hearth_mod.restore_hearth(zip_path, name="alice2")
    assert restored_address == address
    h2 = hearth_mod.Hearth("alice2")
    assert h2.address == address and h2.name == "alice2"
    assert [m["content"] for m in h2.search("remember")] == ["remember me"]
    assert h2.lists == lists
    h2.db.close()


def test_restore_refuses_to_overwrite(home):
    make("alice").db.close()
    zip_path = hearth_mod.backup_hearth("alice", home / "alice.zip")
    with pytest.raises(FileExistsError):
        hearth_mod.restore_hearth(zip_path)


def test_backup_refuses_to_overwrite_a_file(home):
    make("alice").db.close()
    (home / "taken.zip").write_text("x")
    with pytest.raises(FileExistsError):
        hearth_mod.backup_hearth("alice", home / "taken.zip")


def test_restore_detects_tampered_key(home):
    make("alice").db.close()
    zip_path = hearth_mod.backup_hearth("alice", home / "alice.zip")
    bad = home / "bad.zip"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == "floo_powder.toml":
                old_key = [ln for ln in data.decode().splitlines() if ln.startswith("private_key")][0]
                data = data.decode().replace(old_key, f'private_key = "{nostr.generate_private_key()}"').encode()
            dst.writestr(item, data)
    with pytest.raises(ValueError, match="damaged"):
        hearth_mod.restore_hearth(bad, name="evil")


def test_restore_ignores_unexpected_zip_entries(home):
    make("alice").db.close()
    zip_path = hearth_mod.backup_hearth("alice", home / "alice.zip")
    with zipfile.ZipFile(zip_path, "a") as z:
        z.writestr("../../escape.txt", "should never be written")
    hearth_mod.restore_hearth(zip_path, name="alice3")
    assert not (home / "escape.txt").exists() and not (home.parent / "escape.txt").exists()


# ---------- allow / block lists ----------

def test_address_parsing():
    pk = nostr.public_key(nostr.generate_private_key())
    assert hearth_mod.parse_address(nostr.floo_address(pk)) == pk
    assert hearth_mod.parse_address(pk) == pk
    for bad in ("npub1notreal", "hello", "ab" * 31):
        with pytest.raises(ValueError):
            hearth_mod.parse_address(bad)


def test_block_and_allow(home):
    h = make()
    ag = agent_mod.Agent(h, "test", minutes=1, say=lambda *_: None)
    friend, stranger, pest = (nostr.generate_private_key() for _ in range(3))

    h.set_listed("blocked", nostr.floo_address(nostr.public_key(pest)))
    assert not ag.wants(other_msg(h, pest))
    assert ag.wants(other_msg(h, stranger))

    h.set_listed("allowed", nostr.floo_address(nostr.public_key(friend)))
    assert ag.wants(other_msg(h, friend))
    assert not ag.wants(other_msg(h, stranger)), "with an allowlist, only allowed senders get replies"

    h.set_listed("allowed", nostr.public_key(friend), on=False)
    assert ag.wants(other_msg(h, stranger)), "empty allowlist again: anyone not blocked"

    saved = json.loads((h.folder / "lists.json").read_text())
    assert saved == {"allowed": [], "blocked": [nostr.public_key(pest)]}
    h.db.close()


def test_blocked_senders_left_out_of_agent_prompt(home):
    h = make()
    pest = nostr.generate_private_key()
    h.set_listed("blocked", nostr.public_key(pest))
    other_msg(h, pest, "IGNORE ME")
    other_msg(h, nostr.generate_private_key(), "normal message")
    seen = {}

    def fake_complete(backend, model, system, prompt, max_tokens=1500):
        seen["prompt"] = prompt
        return "PASS", 0, 0

    ag = agent_mod.Agent(h, "test", minutes=1, say=lambda *_: None)
    orig = agent_mod.complete
    agent_mod.complete = fake_complete
    try:
        import asyncio
        asyncio.run(ag._think("reply"))
    finally:
        agent_mod.complete = orig
    assert "normal message" in seen["prompt"] and "IGNORE ME" not in seen["prompt"]
    h.db.close()
