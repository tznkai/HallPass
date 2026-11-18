-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A manual mark can wait for the administrator to undo a suspension by hand. restore_pending_since is set when a
-- parent's lift leaves a manually suspended service with no hold, and NULL otherwise. A pending mark has no hold, so
-- holds.py keeps it until the administrator unmarks it or a new hold lands on the service. Rows that exist now are not
-- pending, so they keep their marks unchanged.
ALTER TABLE manual_suspension ADD COLUMN restore_pending_since TEXT;
