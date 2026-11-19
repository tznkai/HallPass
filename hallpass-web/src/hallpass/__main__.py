# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HallPass Console process: `python -m hallpass`.

Exit codes:
- 0: stopped on SIGINT or SIGTERM, after both listeners and the database have closed.
- 2: a bad environment (a setting is missing or invalid).
- 3: the database was refused (quarantined or unreadable).
- 4: a listener failed to start, or ended with SystemExit. The other listener is asked to stop too.
Any other uncaught error exits 1 through Python's default handling.

A stop signal (SIGINT or SIGTERM) asks both listeners to stop, and each lets its in-flight requests finish. A second
stop signal sets `force_exit` on both, which cuts that wait short, as a second Ctrl+C does under uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime

import uvicorn

from hallpass.auth import AuthlibOidc
from hallpass.authentik import AuthentikClient
from hallpass.config import Config, ConfigError
from hallpass.db import Database, DatabaseRefused
from hallpass.logs import log_event, setup_logging
from hallpass.mail import build_sender
from hallpass.monitor_api import create_monitor_app
from hallpass.web import app as web_app

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_BAD_ENVIRONMENT = 2
EXIT_DATABASE_REFUSED = 3
EXIT_LISTENER_FAILED = 4
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class _NoSignalServer(uvicorn.Server):
    """uvicorn's own signal capture re-raises the signal after its shutdown, which ends the process before the cleanup
    runs. `serve_both` owns SIGINT and SIGTERM instead, so neither listener installs a handler."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class PublicServer(_NoSignalServer):
    """The public listener."""


class MonitorServer(_NoSignalServer):
    """The monitor listener. `serve_both` stops it along with the public one."""


def stop_request(servers: Sequence[uvicorn.Server]) -> Callable[[], None]:
    """Returns the handler for SIGINT and SIGTERM. The first signal asks every server to stop. A repeated signal also
    sets `force_exit` on every server, so a slow in-flight request cannot hold the process open."""
    signals_received = 0

    def request_stop() -> None:
        nonlocal signals_received
        signals_received += 1
        for server in servers:
            server.should_exit = True
            if signals_received > 1:
                server.force_exit = True

    return request_stop


async def serve_both(public: uvicorn.Server, monitor: uvicorn.Server) -> int:
    """Runs both listeners until SIGINT or SIGTERM, or until one of them stops. A stop request reaches both servers, and
    a repeated one forces both to exit (see `stop_request`). A listener that ends, normally or by error, asks the other
    to stop. Returns EXIT_LISTENER_FAILED when a listener failed to start (uvicorn exits with SystemExit on a bind
    failure) and EXIT_OK otherwise. Any other exception from a listener is raised after the other listener has
    stopped."""
    loop = asyncio.get_running_loop()
    request_stop = stop_request((public, monitor))

    async def run(server: uvicorn.Server, other: uvicorn.Server) -> int:
        try:
            await server.serve()
        except SystemExit:
            return EXIT_LISTENER_FAILED
        finally:
            other.should_exit = True
        return EXIT_OK

    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, request_stop)
    try:
        results = await asyncio.gather(run(public, monitor), run(monitor, public), return_exceptions=True)
    finally:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return max(results)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_servers(config: Config, deps: web_app.AppDeps) -> tuple[uvicorn.Server, MonitorServer]:
    """The public server, which trusts forwarded headers only from the local reverse proxy, and the monitor listener,
    which trusts none: it refuses any request that carries a proxy header, so it never reads one."""
    public = PublicServer(uvicorn.Config(web_app.create_app(deps), host=config.bind_host, port=config.bind_port,
                                         proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_config=None))
    monitor = MonitorServer(uvicorn.Config(create_monitor_app(deps), host=config.monitor_host,
                                           port=config.monitor_port, proxy_headers=False, log_config=None))
    return public, monitor


async def _serve(config: Config, database: Database) -> int:
    authentik = AuthentikClient(config.authentik_url, config.authentik_token)
    oidc = AuthlibOidc(metadata_url=config.oidc_metadata_url, client_id=config.oidc_client_id,
                       client_secret=config.oidc_client_secret)
    deps = web_app.AppDeps(config=config, db=database, authentik=authentik, oidc=oidc, clock=_utc_now,
                           sender=build_sender(config))
    public, monitor = build_servers(config, deps)
    try:
        return await serve_both(public, monitor)
    finally:
        await authentik.aclose()


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"hallpass: {error}", file=sys.stderr)
        return EXIT_BAD_ENVIRONMENT
    setup_logging(config.log_level, config.secrets())
    try:
        database = Database.open(config.data_dir / "hallpass.db")
    except DatabaseRefused as error:
        print(f"hallpass: {error}", file=sys.stderr)
        return EXIT_DATABASE_REFUSED
    try:
        code = asyncio.run(_serve(config, database))
    finally:
        database.close()
    log_event(logger, "shutdown_complete", code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
