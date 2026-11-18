-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The raw ID token that sign-out sends back to the provider, kept on the server. The session cookie carries only the
-- handle, a random key for this row, so the cookie holds no name, username or groups. oidc_session.py prunes rows older
-- than the session max age on each insert, and sign-out deletes the row it takes.
CREATE TABLE oidc_session (
    handle TEXT PRIMARY KEY,
    id_token TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
