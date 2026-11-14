# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SteamID64 to Source engine ID conversion. Every SteamID64 here is built at runtime from the base, never written out."""

from __future__ import annotations

import pytest

from hallmonitor.adapters.steam import STEAM64_BASE, steam2, valid_steam64


def _id64(offset: int) -> str:
    return str(STEAM64_BASE + offset)


def test_base_is_a_seventeen_digit_steamid64_with_the_public_individual_prefix():
    text = str(STEAM64_BASE)
    assert len(text) == 17
    assert text.startswith("7656119")


@pytest.mark.parametrize(
    ("offset", "universe", "expected"),
    [
        (0, "0", "STEAM_0:0:0"),          # the account number 0, even parity
        (1, "0", "STEAM_0:1:0"),          # odd parity sets the x digit
        (2 * 123 + 1, "0", "STEAM_0:1:123"),
        (2 * 123, "1", "STEAM_1:0:123"),  # the universe digit is passed through as given
        (2 * 4567, "0", "STEAM_0:0:4567"),
    ],
)
def test_steam2_gives_the_source_id_for_an_offset_from_the_base(offset: int, universe: str, expected: str):
    assert steam2(_id64(offset), universe) == expected


def test_steam2_rejects_a_universe_that_is_not_one_digit():
    with pytest.raises(ValueError):
        steam2(_id64(0), "10")
    with pytest.raises(ValueError):
        steam2(_id64(0), "x")


def test_steam2_rejects_a_steam_id_that_is_not_decimal_digits():
    with pytest.raises(ValueError):
        steam2("12a", "0")
    with pytest.raises(ValueError):
        steam2("", "0")
    with pytest.raises(ValueError):
        steam2("+" + _id64(0), "0")


def test_steam2_rejects_an_id_below_the_base_without_echoing_it():
    below = str(STEAM64_BASE - 2)
    with pytest.raises(ValueError) as info:
        steam2(below, "0")
    assert below not in str(info.value)


@pytest.mark.parametrize("offset", [0, 1, 2 * 4567])
def test_valid_steam64_accepts_decimal_ids_at_or_above_the_base(offset: int):
    assert valid_steam64(_id64(offset))


@pytest.mark.parametrize(
    "value",
    ["", "12a", " " + "1", "+1", "1_0", "\u0661", "-1", "0" * 40, "9" * 25, str(STEAM64_BASE - 1)],
)
def test_valid_steam64_refuses_anything_else_without_raising(value: str):
    assert not valid_steam64(value)
