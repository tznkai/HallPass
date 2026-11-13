# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI app factory. Dependencies arrive as one AppDeps object, so tests build the app over fakes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI

from hallpass.auth import OidcClient, RedirectToLogin, add_session_middleware, csrf_token
from hallpass.authentik import AuthentikClient
from hallpass.config import Config
from hallpass.db import Database
from hallpass.registry import NoSuchGroup, NoSuchPerson
from hallpass.web.pages import make_templates, not_found, redirect_to_login
from hallpass.web.routes_admin import router as admin_router
from hallpass.web.routes_auth import router as auth_router


@dataclass(frozen=True)
class AppDeps:
    config: Config
    db: Database
    authentik: AuthentikClient
    oidc: OidcClient
    clock: Callable[[], datetime]


def create_app(deps: AppDeps) -> FastAPI:
    # The API docs are not served: hallpass has no JSON API for browsers to explore.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.deps = deps
    templates = make_templates()
    templates.env.globals["csrf_token"] = csrf_token
    app.state.templates = templates
    add_session_middleware(app, deps.config)
    app.add_exception_handler(RedirectToLogin, redirect_to_login)
    app.add_exception_handler(NoSuchPerson, not_found)
    app.add_exception_handler(NoSuchGroup, not_found)
    app.include_router(auth_router)
    app.include_router(admin_router)
    return app
