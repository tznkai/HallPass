# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI app factory. Dependencies arrive as one AppDeps object, so tests build the app over fakes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI

from hallpass.auth import OidcClient, RedirectToLogin, RedirectToTerms, add_session_middleware, csrf_token
from hallpass.authentik import AuthentikClient
from hallpass.authentik_sync import resync_loop
from hallpass.config import Config
from hallpass.db import Database
from hallpass.forward_auth import router as forward_auth_router
from hallpass.mail import Sender, mail_loop
from hallpass.registry import NoSuchGroup, NoSuchPerson
from hallpass.services import NoSuchService
from hallpass.web.pages import make_templates, not_found, redirect_to_login, redirect_to_terms
from hallpass.web.routes_admin import router as admin_router
from hallpass.web.routes_auth import router as auth_router
from hallpass.web.routes_me import router as me_router
from hallpass.web.routes_family import router as family_router
from hallpass.web.routes_terms import router as terms_router


@dataclass(frozen=True)
class AppDeps:
    config: Config
    db: Database
    authentik: AuthentikClient
    oidc: OidcClient
    clock: Callable[[], datetime]
    sender: Sender
    # When True, the lifespan runs the mail delivery and the Authentik resync loops. Tests turn it off.
    background: bool = True


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    deps: AppDeps = app.state.deps
    tasks: list[asyncio.Task] = []
    if deps.background:
        tasks = [asyncio.create_task(mail_loop(deps)), asyncio.create_task(resync_loop(deps))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(deps: AppDeps) -> FastAPI:
    # The API docs are not served: the Console has no JSON API for browsers to explore.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.deps = deps
    templates = make_templates()
    templates.env.globals["csrf_token"] = csrf_token
    app.state.templates = templates
    add_session_middleware(app, deps.config)
    app.add_exception_handler(RedirectToLogin, redirect_to_login)
    app.add_exception_handler(RedirectToTerms, redirect_to_terms)
    app.add_exception_handler(NoSuchPerson, not_found)
    app.add_exception_handler(NoSuchGroup, not_found)
    app.add_exception_handler(NoSuchService, not_found)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(terms_router)
    app.include_router(admin_router)
    app.include_router(family_router)
    app.include_router(forward_auth_router)
    return app
