# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one rule for which characters a display name may hold.

A display name is written into email subjects, and Python's email header check refuses every line or paragraph
separator as it refuses CR and LF. So the rule covers Unicode categories Cc (control characters), Zl (LINE SEPARATOR,
U+2028) and Zp (PARAGRAPH SEPARATOR, U+2029). The registry refuses such a name; the admin bootstrap strips it.
Both call `is_unsafe`, so they cannot drift apart.
"""

from __future__ import annotations

import unicodedata

UNSAFE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def is_unsafe(ch: str) -> bool:
    return unicodedata.category(ch) in UNSAFE_CATEGORIES
