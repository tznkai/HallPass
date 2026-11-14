# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SteamID64 to the Source engine's STEAM_x:y:z form. The base is computed, so no SteamID64 literal is written here."""

from __future__ import annotations

import re

# The SteamID64 base: universe public (1 << 56), account type individual (1 << 52), instance 1 (1 << 32).
STEAM64_BASE = (1 << 56) | (1 << 52) | (1 << 32)
# The individual account range is the base plus the 32-bit account number, so the limit is exclusive.
STEAM64_LIMIT = STEAM64_BASE + (1 << 32)
UNIVERSE_DIGITS = frozenset("012345")  # the digit in STEAM_[0-5], as the engine prints it
DECIMAL = re.compile(r"[0-9]+")
MAX_DIGITS = 20  # a 64-bit integer has at most 20 decimal digits


def steam2(steam_id64: str, universe: str) -> str:
    """The Source engine ID for a canonical SteamID64 decimal string. A value that is not canonical is refused with a
    message that never echoes the id."""
    if universe not in UNIVERSE_DIGITS:
        raise ValueError("universe must be one digit from 0 to 5")
    if not valid_steam64(steam_id64):
        raise ValueError("steam id must be a canonical SteamID64 in the individual range")
    value = int(steam_id64)
    return f"STEAM_{universe}:{value % 2}:{(value - STEAM64_BASE) // 2}"


def valid_steam64(steam_id64: str) -> bool:
    """True for a canonical decimal SteamID64 in the individual range: at or above the base and below the base plus 2^32.
    The digits must round-trip through int, so a leading zero is refused. Adapters check an identity with this before it
    reaches a request or a file line. Anything else is False and never raises."""
    if len(steam_id64) > MAX_DIGITS or DECIMAL.fullmatch(steam_id64) is None:
        return False
    value = int(steam_id64)
    return str(value) == steam_id64 and STEAM64_BASE <= value < STEAM64_LIMIT
