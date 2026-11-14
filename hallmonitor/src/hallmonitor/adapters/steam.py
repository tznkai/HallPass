# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SteamID64 to the Source engine's STEAM_x:y:z form. The base is computed, so no SteamID64 literal is written here."""

from __future__ import annotations

import re

# The SteamID64 base: universe public (1 << 56), account type individual (1 << 52), instance 1 (1 << 32).
STEAM64_BASE = (1 << 56) | (1 << 52) | (1 << 32)
UNIVERSE_DIGITS = frozenset("012345")  # the digit in STEAM_[0-5], as the engine prints it
DECIMAL = re.compile(r"[0-9]+")


def steam2(steam_id64: str, universe: str) -> str:
    """The Source engine ID for a SteamID64 decimal string. A value that is not decimal, or is below the base, is refused
    with a message that never echoes the id."""
    if universe not in UNIVERSE_DIGITS:
        raise ValueError("universe must be one digit from 0 to 5")
    if DECIMAL.fullmatch(steam_id64) is None:
        raise ValueError("steam id must be decimal digits")
    value = int(steam_id64)
    if value < STEAM64_BASE:
        raise ValueError("steam id is below the SteamID64 base")
    return f"STEAM_{universe}:{value % 2}:{(value - STEAM64_BASE) // 2}"
