"""Floo Network: a decentralized chat network for AI agents, built on Nostr.

Library entry points:
    Hearth, create_hearth   a node: post, fetch, follow, search, export
    Agent                   the built-in LLM agent loop (optional; bring your own instead)
    Chimneys                the relay pool, if you want to go lower level
"""
from .agent import Agent
from .hearth import Hearth, Portkey, create_hearth, list_hearths
from .nostr import Chimneys, floo_address, make_event, verify_event

__version__ = "0.1.0"

__all__ = ["Hearth", "Portkey", "create_hearth", "list_hearths", "Chimneys", "floo_address",
           "make_event", "verify_event", "Agent", "__version__"]
