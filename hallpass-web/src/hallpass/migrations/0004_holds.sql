-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE review_item (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by INTEGER REFERENCES person(id),
    outcome TEXT
);

CREATE TABLE hold (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('parent', 'admin', 'lockdown')),
    placed_by INTEGER REFERENCES person(id),
    placed_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    review_id INTEGER REFERENCES review_item(id),
    CHECK ((kind = 'lockdown') = (review_id IS NOT NULL))
);
CREATE UNIQUE INDEX hold_admin_one ON hold(person_id, service_id) WHERE kind = 'admin';
CREATE UNIQUE INDEX hold_parent_per_parent ON hold(person_id, service_id, placed_by) WHERE kind = 'parent';
CREATE UNIQUE INDEX hold_lockdown_one ON hold(person_id, service_id, review_id) WHERE kind = 'lockdown';

CREATE TABLE manual_suspension (
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    marked_at TEXT NOT NULL,
    marked_by INTEGER REFERENCES person(id),
    PRIMARY KEY (person_id, service_id)
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
CREATE INDEX outbox_due ON outbox(sent_at, next_attempt_at);
