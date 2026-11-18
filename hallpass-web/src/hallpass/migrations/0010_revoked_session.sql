-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Back-channel logout and sign-out (phase 8). A revoked_session row ends the one Authentik session whose sid it names.
-- A revoked_subject row ends every hallpass session of one person (sub = the person's authentik_uuid) that started
-- before revoked_at, for a logout token that carries no sid. revocation.py prunes rows older than the session max age
-- on each insert: a cookie signed before then has expired by its own max age, so no live session can still need one.
CREATE TABLE revoked_session (
    sid TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL
) STRICT;

CREATE TABLE revoked_subject (
    sub TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL
) STRICT;
