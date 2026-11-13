# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hallpass process: `python -m hallpass`. Exit 2 for a bad environment, 3 for a refused database."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import uvicorn

from hallpass.auth import AuthlibOidc
from hallpass.authentik import AuthentikClient
from hallpass.config import Config, ConfigError
from hallpass.db import Database, DatabaseRefused
from hallpass.logs import setup_logging
from hallpass.web import app as web_app


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _serve(config: Config, database: Database) -> None:
    authentik = AuthentikClient(config.authentik_url, config.authentik_token)
    oidc = AuthlibOidc(metadata_url=config.oidc_metadata_url, client_id=config.oidc_client_id,
                       client_secret=config.oidc_client_secret)
    deps = web_app.AppDeps(config=config, db=database, authentik=authentik, oidc=oidc, clock=_utc_now)
    # The server trusts forwarded headers only from the local reverse proxy.
    server = uvicorn.Server(uvicorn.Config(web_app.create_app(deps), host=config.bind_host, port=config.bind_port, proxy_headers=True,
                                           forwarded_allow_ips="127.0.0.1", log_config=None))
    try:
        await server.serve()
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
