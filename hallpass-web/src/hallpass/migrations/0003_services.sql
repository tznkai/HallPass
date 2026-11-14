-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE service (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE CHECK (slug GLOB '[a-z0-9]*' AND slug NOT GLOB '*[^a-z0-9_-]*'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('oidc', 'forward_auth', 'game', 'feed')),
    adapter TEXT NOT NULL DEFAULT 'manual',
    auto_suspendable INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    retired INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE entitlement (
    id INTEGER PRIMARY KEY,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('type', 'group', 'person')),
    subject TEXT NOT NULL,
    effect TEXT NOT NULL CHECK (effect IN ('grant', 'deny')),
    UNIQUE (service_id, subject_kind, subject),
    CHECK (effect = 'grant' OR subject_kind = 'person')
);

CREATE TABLE token (
    id INTEGER PRIMARY KEY,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES person(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('feed', 'invite_link', 'game_credential', 'api_key')),
    label TEXT NOT NULL DEFAULT '',
    value TEXT,
    hash TEXT UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK ((kind = 'feed') = (hash IS NOT NULL AND value IS NULL AND person_id IS NOT NULL))
);
CREATE INDEX token_person ON token(person_id);
