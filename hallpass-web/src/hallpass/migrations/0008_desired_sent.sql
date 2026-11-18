-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A (person, service) pair that stops being a subject keeps its desired_state row with sent = 0, instead of losing it.
-- The row keeps the generation on record, so a pair that returns starts above it (desired.py `_store_generation`), and
-- a late report carrying the old generation is never confirmed. A row with sent = 0 is not sent and is never read by
-- desired_for, so no report is stored for it. Rows that exist now are subjects as of the last refresh, so they start
-- as sent.
ALTER TABLE desired_state ADD COLUMN sent INTEGER NOT NULL DEFAULT 1 CHECK (sent IN (0, 1));
