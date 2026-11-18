# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared page style: the colour scheme declared for each theme, and form controls that read in both themes."""

from __future__ import annotations

import re

from tests.helpers import Env, signed_in_person


def style_of(page: str) -> str:
    start = page.index("<style>") + len("<style>")
    return page[start:page.index("</style>", start)]


def rules(css: str) -> list[tuple[set[str], str]]:
    """Each innermost rule as (its selector names, its declarations). Rules inside @media are included."""
    found = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        found.append(({part.strip() for part in selector.split(",")}, body))
    return found


def rule_for(css: str, *names: str) -> str:
    """The declarations of the first rule whose selector list names every one of these, or an empty string."""
    for selector_names, body in rules(css):
        if set(names) <= selector_names:
            return body
    return ""


async def home_page(env: Env) -> str:
    _, browser = await signed_in_person(env)
    return (await browser.get("/")).text


# The colour scheme: the default follows the system, and each forced theme names its own ------------------

async def test_the_default_and_both_forced_themes_declare_their_colour_scheme(env: Env):
    css = style_of(await home_page(env))

    assert "color-scheme: light dark" in rule_for(css, ":root")
    assert "color-scheme: dark" in rule_for(css, ':root[data-theme="dark"]')
    assert "color-scheme: light" in rule_for(css, ':root[data-theme="light"]')


# Form controls: the native option list and the other controls take the page's colours --------------------

async def test_the_select_and_its_options_use_the_page_background_and_text(env: Env):
    css = style_of(await home_page(env))

    options = rule_for(css, "select", "option", "optgroup")

    assert "background: var(--bg)" in options and "color: var(--fg)" in options


async def test_text_inputs_and_textareas_use_the_page_background_and_text(env: Env):
    css = style_of(await home_page(env))

    fields = rule_for(css, "input", "select", "textarea")

    assert "background: var(--bg)" in fields and "color: var(--fg)" in fields


async def test_buttons_use_the_page_background_rather_than_the_browser_default(env: Env):
    css = style_of(await home_page(env))

    assert "background: var(--bg)" in rule_for(css, "button")
