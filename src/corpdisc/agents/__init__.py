"""Reasoning plane: agent contracts, runner, gateway, broker and providers.

Import rule (ARCHITECTURE 28.1): agents/ never imports db/ or services/fetch; it sees the
database and network only through protocols in ``agents.ports`` implemented elsewhere.
"""
