from floo import nostr


def test_sign_and_verify():
    sk = nostr.generate_private_key()
    ev = nostr.make_event(sk, 4141, "hello ✨ unicode", [["t", "floo-general"]])
    assert ev["pubkey"] == nostr.public_key(sk)
    assert nostr.verify_event(ev)


def test_tampering_breaks_verification():
    ev = nostr.make_event(nostr.generate_private_key(), 4141, "original")
    assert not nostr.verify_event({**ev, "content": "edited"})
    other = nostr.make_event(nostr.generate_private_key(), 4141, "original", created_at=ev["created_at"])
    assert not nostr.verify_event({**ev, "pubkey": other["pubkey"]})


def test_npub_known_vector():
    # NIP-19 example
    pk = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    assert nostr.floo_address(pk) == "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    assert nostr.bech32_decode(nostr.floo_address(pk)) == ("npub", pk)
