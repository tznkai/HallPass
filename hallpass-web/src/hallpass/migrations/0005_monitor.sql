-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
CREATE TABLE desired_state (
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    desired TEXT NOT NULL CHECK (desired IN ('allowed', 'suspended')),
    generation INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY (person_id, service_id)
);

CREATE TABLE service_report (
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    observed TEXT NOT NULL CHECK (observed IN ('allowed', 'suspended', 'unsupported', 'banned_external', 'error')),
    detail TEXT,
    generation INTEGER NOT NULL,
    reported_at TEXT NOT NULL,
    PRIMARY KEY (person_id, service_id)
);

CREATE TABLE service_unmanaged (
    service_id INTEGER PRIMARY KEY REFERENCES service(id) ON DELETE CASCADE,
    count INTEGER NOT NULL,
    reported_at TEXT NOT NULL
);

CREATE TABLE monitor_contact (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_contact_at TEXT NOT NULL
);
