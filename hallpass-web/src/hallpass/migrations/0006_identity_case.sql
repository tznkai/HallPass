-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Minecraft names compare without case (identity_key in registry.py). This index refuses a java_name or
-- bedrock_gamertag that differs from a recorded one only by case. The existing UNIQUE (service_slug, kind, value)
-- stays as it is. If existing rows already collide, this statement fails and startup stops with an uncaught error
-- (exit 1), as for any failed migration: the database keeps version 5 and the pre-migration backup is written.
-- Nothing is merged automatically; an operator must remove or correct one row of each colliding pair first.
CREATE UNIQUE INDEX external_identity_case ON external_identity (
    service_slug,
    kind,
    CASE WHEN kind IN ('java_name', 'bedrock_gamertag') THEN lower(value) ELSE value END
);
