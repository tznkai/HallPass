<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# HallPass

## Human to Human

## Overview

HallPass is a registry and authorization system for neighbourhood services. It records who may use which service, and
it applies those decisions to the services themselves, so that a person's access to a game server or to Fluxer follows
what the administrator and the families decided, and a change made in one place takes effect everywhere it applies.

HallPass has two components. The HallPass Console ([`hallpass-web/`](hallpass-web/README.md)) is the web app. It signs
residents in through Authentik, keeps the registry of people, services, groups, terms and holds, and publishes the
desired state of who may use what. It holds no service credentials. HallMonitor ([`hallmonitor/`](hallmonitor/README.md))
reads that desired state and applies it to Fluxer and to the game servers, so that each service matches the decisions
the Console holds. HallMonitor holds the service credentials, has no inbound ports, and makes outbound calls only.

The two deploy together on one host. The Console runs behind Caddy, which asks the Console through forward auth whether
each request to a protected service may pass. HallMonitor runs on the same host and reaches the Console's monitor API
over loopback. The monitor listener is never proxied.

Authentik is required. The Console signs people in through an Authentik OpenID Connect provider, and it uses Authentik's
API for invitations, for the Fluxer group and for ending sessions. Without Authentik, nobody can sign in and no access
can be decided.

For setup, read each component's README: [`hallpass-web/README.md`](hallpass-web/README.md) for the Console (its
Authentik provider, Caddy and configuration) and [`hallmonitor/README.md`](hallmonitor/README.md) for HallMonitor (its
adapters and their settings).
