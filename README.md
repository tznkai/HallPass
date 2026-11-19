<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# HallPass

## Human to Human
Hi, human author here. I wrote this section of the README myself so you don't have get through Claudese to understand what's happening. This project tries to address the problem "what if my kids want to play online games with their neighbors and school friends but I don't want them simultaneously exposed every stranger on the internet?" My solution to that problem is self-hosting games with a whitelist-first approach and giving other parents the ability to give and revoke permission. The particulars are me-shaped, optimized for the software stack sitting on my homelab, but it might be useful to you and your particulars with some light modification.

Outside of this section of the README, including the rest of this file, this project, was directed by K.Chen through iterative instruction and testing, orchestrated by Claude Code using Opus 5.5, and audited and coded by various Claude models of that generation. This presents certain risks to you. Your best way to alleviate those risks is what it always is with free and open source software: audit the code yourself, and fork as required.

## Overview

HallPass is a registry and authorization system for neighbourhood services. It records who may use which service, and
it applies those decisions to the services themselves, so that a person's access to a game server or to Fluxer follows
what the administrator and the families decided, and a change made in one place takes effect everywhere it applies.

HallPass has two components. The HallPass Console ([`hallpass-web/`](hallpass-web/README.md)) is the web app. It signs
residents in through Authentik, keeps the registry of people, services, groups, terms and holds, and publishes the
desired state of who may use what. It holds no service credentials. HallMonitor
([`hallmonitor/`](hallmonitor/README.md)) reads that desired state and applies it to Fluxer and to the game servers,
so that each service matches the decisions the Console holds. HallMonitor holds the service credentials, has no inbound
ports, and makes outbound calls only.

The two deploy together on one host. The Console runs behind Caddy, which asks the Console through forward auth whether
each request to a protected service may pass. HallMonitor runs on the same host and reaches the Console's monitor API
over loopback. The monitor listener is never proxied.

Authentik is required. The Console signs people in through an Authentik OpenID Connect provider, and it uses Authentik's
API for invitations, for the Fluxer group and for ending sessions. Without Authentik, nobody can sign in and no access
can be decided.

For setup, read each component's README: [`hallpass-web/README.md`](hallpass-web/README.md) for the Console (its
Authentik provider, Caddy and configuration) and [`hallmonitor/README.md`](hallmonitor/README.md) for HallMonitor (its
adapters and their settings).
