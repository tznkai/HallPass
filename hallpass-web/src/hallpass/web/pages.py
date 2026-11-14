# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page rendering and the exception handlers that turn hallpass errors into pages or redirects."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from hallpass import text
from hallpass.auth import RedirectToLogin, RedirectToTerms
from hallpass.registry import NoSuchGroup, NoSuchPerson, Person

TEMPLATES_DIR = Path(__file__).parent / "templates"


def make_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=TEMPLATES_DIR)


def page(request: Request, name: str, *, viewer: Person | None, status_code: int = 200,
         **context: object) -> HTMLResponse:
    """Renders a template. `viewer` is the signed-in person, or None; it decides the nav and the admin links."""
    deps = request.app.state.deps
    context["viewer"] = viewer
    context["is_admin"] = viewer is not None and viewer.authentik_uuid in deps.config.admin_subs
    context["has_children"] = viewer is not None and bool(getattr(request.state, "has_children", False))
    context["open_reviews"] = getattr(request.state, "open_reviews", 0)
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)


def message_page(request: Request, message: str, status_code: int) -> HTMLResponse:
    return page(request, "message.html", viewer=None, status_code=status_code, message=message)


async def redirect_to_login(request: Request, exc: RedirectToLogin) -> RedirectResponse:
    return RedirectResponse(f"/login?{urlencode({'next': exc.next_path})}", status_code=303)


async def redirect_to_terms(request: Request, exc: RedirectToTerms) -> RedirectResponse:
    return RedirectResponse(f"/terms?{urlencode({'next': exc.next_path})}", status_code=303)


async def not_found(request: Request, exc: NoSuchPerson | NoSuchGroup) -> HTMLResponse:
    return message_page(request, text.NOT_FOUND, 404)
