# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The app's background loops: started by the lifespan when background is on, and cancelled on shutdown.

Covers hallpass.AC13.5 (the loop delivers what the decision queued) through the app's own lifespan.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

from hallpass import mail
from tests.helpers import NOW, make_env


async def test_the_lifespan_delivers_queued_mail_and_stops_its_tasks_on_shutdown(tmp_path: Path):
    env = await make_env(tmp_path)
    try:
        env.application.state.deps = dataclasses.replace(env.application.state.deps, background=True)
        await env.db.run(lambda c: mail.enqueue(c, to_addr="parent@example.com", subject="Hallpass: test",
                                                body="A change was made.", now=NOW))

        async with env.application.router.lifespan_context(env.application):
            for _ in range(200):
                if env.sender.calls:
                    break
                await asyncio.sleep(0.01)

        assert [call[0] for call in env.sender.calls] == ["parent@example.com"]
    finally:
        await env.close()


async def test_without_background_the_lifespan_starts_no_loop(tmp_path: Path):
    env = await make_env(tmp_path)
    try:
        await env.db.run(lambda c: mail.enqueue(c, to_addr="parent@example.com", subject="Hallpass: test",
                                                body="A change was made.", now=NOW))

        async with env.application.router.lifespan_context(env.application):
            await asyncio.sleep(0.05)

        assert env.sender.calls == []
    finally:
        await env.close()
