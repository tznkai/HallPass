-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Several people may share one identity on a service (desired.py pauses a shared identity while any holder is
-- suspended). The table is rebuilt without the cross-person UNIQUE (service_slug, kind, value) and without the
-- cross-person case index from 0006, which this replaces. Row ids are kept. What stays unique:
--   * a person holds an identity once per service and kind, case-folded for java_name and bedrock_gamertag
--     (the same rule as identity_key in registry.py: change both together);
--   * a fluxer_user_id belongs to one person on its service.
-- The expressions below are the SQL form of registry.identity_key.
CREATE TABLE external_identity_new (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_slug TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fluxer_user_id', 'java_name', 'bedrock_gamertag', 'steam_id')),
    value TEXT NOT NULL
);
INSERT INTO external_identity_new (id, person_id, service_slug, kind, value)
    SELECT id, person_id, service_slug, kind, value FROM external_identity;
DROP TABLE external_identity;
ALTER TABLE external_identity_new RENAME TO external_identity;
CREATE UNIQUE INDEX external_identity_person ON external_identity (
    person_id,
    service_slug,
    kind,
    CASE WHEN kind IN ('java_name', 'bedrock_gamertag') THEN lower(value) ELSE value END
);
CREATE UNIQUE INDEX external_identity_fluxer_user ON external_identity (service_slug, value)
    WHERE kind = 'fluxer_user_id';
