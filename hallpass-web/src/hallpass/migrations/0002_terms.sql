-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE terms_version (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    tos_text TEXT NOT NULL,
    privacy_text TEXT NOT NULL,
    blocking INTEGER NOT NULL DEFAULT 1,
    published_at TEXT NOT NULL,
    published_by INTEGER REFERENCES person(id)
);

CREATE TABLE terms_acceptance (
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    terms_version_id INTEGER NOT NULL REFERENCES terms_version(id),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (person_id, terms_version_id)
);

CREATE TABLE parent_grant (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES person(id),
    terms_version_id INTEGER NOT NULL REFERENCES terms_version(id),
    granted_at TEXT NOT NULL
);
