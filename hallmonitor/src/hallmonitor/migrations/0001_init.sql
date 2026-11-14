-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE own_suspension (
    slug TEXT NOT NULL,
    identity TEXT NOT NULL,
    own_ref TEXT NOT NULL DEFAULT '',
    placed_at TEXT NOT NULL,
    PRIMARY KEY (slug, identity)
);
