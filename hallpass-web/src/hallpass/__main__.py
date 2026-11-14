# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hallpass process: `python -m hallpass`. Exit 2 for a bad environment, 3 for a refused database."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import uvicorn

from hallpass.auth import AuthlibOidc
from hallpass.authentik import AuthentikClient
from hallpass.config import Config, ConfigError
from hallpass.db import Database, DatabaseRefused
from hallpass.logs import setup_logging
from hallpass.mail import build_sender
from hallpass.monitor_api import create_monitor_app
from hallpass.web import app as web_app


class MonitorServer(uvicorn.Server):
    """The monitor listener installs no signal handlers. The public server owns SIGINT and SIGTERM and stops this one
    through `serve_both`, so the two servers never overwrite each other's handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve_both(public: uvicorn.Server, monitor: uvicorn.Server) -> None:
    """Runs both listeners. When either one ends, normally or by error, the other is asked to stop."""
    async def run(server: uvicorn.Server, other: uvicorn.Server) -> None:
        try:
            await server.serve()
        finally:
            other.should_exit = True

    await asyncio.gather(run(public, monitor), run(monitor, public))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_servers(config: Config, deps: web_app.AppDeps) -> tuple[uvicorn.Server, MonitorServer]:
    """The public server, which trusts forwarded headers only from the local reverse proxy, and the monitor listener,
    which trusts none: it refuses any request that carries a proxy header, so it never reads one."""
    public = uvicorn.Server(uvicorn.Config(web_app.create_app(deps), host=config.bind_host, port=config.bind_port,
                                           proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_config=None))
    monitor = MonitorServer(uvicorn.Config(create_monitor_app(deps), host=config.monitor_host,
                                           port=config.monitor_port, proxy_headers=False, log_config=None))
    return public, monitor


async def _serve(config: Config, database: Database) -> None:
    authentik = AuthentikClient(config.authentik_url, config.authentik_token)
    oidc = AuthlibOidc(metadata_url=config.oidc_metadata_url, client_id=config.oidc_client_id,
                       client_secret=config.oidc_client_secret)
    deps = web_app.AppDeps(config=config, db=database, authentik=authentik, oidc=oidc, clock=_utc_now,
                           sender=build_sender(config))
    public, monitor = build_servers(config, deps)
    try:
        await serve_both(public, monitor)
    finally:
        await authentik.aclose()


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"hallpass: {error}", file=sys.stderr)
        return 2
    setup_logging(config.log_level, config.secrets())
    try:
        database = Database.open(config.data_dir / "hallpass.db")
    except DatabaseRefused as error:
        print(f"hallpass: {error}", file=sys.stderr)
        return 3
    try:
        asyncio.run(_serve(config, database))
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
