-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    ref TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('adult', 'parent', 'child')),
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'awaiting_grant', 'active', 'locked')),
    prior_status TEXT,
    authentik_uuid TEXT UNIQUE,
    authentik_pk INTEGER,
    username TEXT,
    email TEXT,
    phone TEXT,
    fluxer_email_override TEXT,
    enrollment_ref TEXT UNIQUE,
    invitation_pk TEXT,
    invite_url TEXT,
    conflict_mode TEXT NOT NULL DEFAULT 'default'
        CHECK (conflict_mode IN ('cooperative', 'default', 'monitored', 'strict')),
    agreement_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE parent_link (
    child_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (child_id, parent_id),
    CHECK (child_id <> parent_id)
);

CREATE TABLE grp (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE grp_member (
    grp_id INTEGER NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    PRIMARY KEY (grp_id, person_id)
);

CREATE TABLE external_identity (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_slug TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fluxer_user_id', 'java_name', 'bedrock_gamertag', 'steam_id')),
    value TEXT NOT NULL,
    UNIQUE (service_slug, kind, value)
);

CREATE TABLE decision_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('admin', 'parent', 'person', 'system')),
    actor_person_id INTEGER REFERENCES person(id),
    target_person_id INTEGER REFERENCES person(id),
    action TEXT NOT NULL,
    services TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '{}',
    reversal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX decision_log_target ON decision_log(target_person_id, at);
