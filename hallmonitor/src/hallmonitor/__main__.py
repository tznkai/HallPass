# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HallMonitor process: `python -m hallmonitor`. Exit 2 for a bad environment, 3 for a refused database."""

from __future__ import annotations

import asyncio
import sys

from hallmonitor.config import Config, ConfigError
from hallmonitor.db import DatabaseRefused
from hallmonitor.reconcile import run


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"hallmonitor: {error}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config))
    except DatabaseRefused as error:
        print(f"hallmonitor: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
