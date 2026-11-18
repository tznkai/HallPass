<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Service notes

Live-check results for each service hallmonitor touches. Hosts, ids and keys are placeholders such as
`<fluxer-api>` and `<id>`. Source references are to the public Fluxer repository's main branch as read on
2026-11-13; the instance under test may run a slightly different build.

## Fluxer Admin API

Run 2026-11-13 with `tools/live/fluxer_check.py` against a dedicated test account.

- **Base and auth.** `<fluxer-api>/v1/admin/...`, where `<fluxer-api>` is `endpoints.api_public` from
  `<instance>/.well-known/fluxer` without the trailing `/v1`. Headers: `Authorization: Admin <key>` and
  `X-Audit-Log-Reason: <text>` (the audit header is required on enforcement routes).
- **Lookup.** `GET /v1/admin/users/{id}` -> 200 with a wrapped body, `{"users": [<user>]}`.
  - `flags` is a **string** holding a 64-bit integer. Parse it with `int()`, never as a float.
  - DISABLED is `1 << 38`. `1 << 39` is `HAS_SESSION_STARTED` and is normal on an account that has signed in.
  - `temp_banned_until` and `enforcement_decision_id` are null on an account with no suspension.
  - The user object also carries `email`, `date_of_birth`, `last_active_ip`, `last_active_location` and more.
    Hallmonitor reads only `id` and `flags` from it and never logs or stores any other field.
- **Sessions.**
  - `GET /v1/admin/users/{id}/sessions` -> `{"sessions": [...]}`. The list also holds ended sessions: an entry with
    `deleted_at` set has been terminated, and only entries with `deleted_at` null are active.
  - `DELETE /v1/admin/users/{id}/sessions` -> 200 `{"terminated_count": <n>}`. Observed: 2 active before, 2
    terminated, 0 active after, and the test client was signed out at once. It needs `user:update_flags`.
- **Suspension needs an enforcement decision.** `PUT /v1/admin/users/{id}/ban` with
  `{"duration_hours": 0, "decision": {}}` -> 400. In the source the body is
  `{duration_hours, decision: {reason_code, source, automation, statement?, ...}}`, where:
  - `reason_code` must be a rule-violation code (spam, harassment, ban evasion and so on). The neutral codes
    (user requested, other, inactivity) are excluded;
  - `source` is `report` or `own_review`, and `automation` is `none` or `flagged`;
  - the decision is recorded on the account and sends the user an enforcement notice by email and system DM.
- **No neutral disable.** The flags endpoint refuses to add DISABLED (`FLAG_REQUIRES_ENFORCEMENT_ROUTE`), so the
  ban route is the only way to set it.
- **Lift.** Not exercised. In the source a decision ends with
  `POST /v1/admin/enforcement-decisions/{id}/end` and `{"outcome": "lifted" | "reversed", "statement"?}`.
- **SSO identity lookup.**
  - `GET /v1/admin/users/{id}/sso-identities` -> `{"sso_identities": [{provider_id, subject, user_id, claimed_at}]}`.
  - `GET /v1/admin/sso-identities?provider_id=<provider_id>&subject=<sub>` -> `{"sso_identity": {...} | null}`, and
    returned the right Fluxer user. Both query parameters are required (subject alone -> 400). It needs `user:lookup`.
- **ACLs.** `GET /v1/admin/api-keys` -> 403 for this key, so the key's own ACL list is not visible through it.
  With the design change below the key needs `user:lookup`, `user:list_sessions` and `user:update_flags`
  (Fluxer files session termination under `update_flags`).

## Fluxer SSO

Run 2026-11-13: Fluxer SSO against an Authentik OIDC provider over HTTPS, with "Require SSO" off.

- **Instance identity mode:** email (`features.account_identity` in `<instance>/.well-known/fluxer`).
- **Issuer requirements.** Fluxer refuses an `http://` issuer, and refuses a host that resolves to a private
  address unless `FLUXER_SSO_ALLOW_PRIVATE_ADDRESSES=true` is set on the Fluxer server.
  The callback is `<fluxer web app>/auth/sso/callback`.
- **Provider settings.** Subject mode "Based on the User's UUID"; an `email` scope mapping returning the
  `fluxer_email` attribute with `email_verified: True`; the Authentik application bound to a `fluxer` group.
- **Plus-address.** A first SSO sign-in with a plus-address presented as verified created a new account, whose
  email is the plus-address and is verified. On an instance with approval-mode registration the new account
  waits for approval like any other sign-up. Tested with "Allowed Email Domains" empty.
- **Subject binding.** Fluxer binds the account to `sub`, and the stored `subject` equals Authentik's bare uuid.
  A later sign-in with a known `sub` goes straight to that account and ignores the email.
- **Linking an existing account.** A new `sub` whose verified email matches an existing account with a verified
  email signed straight into that account and bound it. Per the source it refuses instead
  (`SSO_ACCOUNT_LINK_REQUIRES_SIGN_IN`) when that account's email is unverified or the instance has email off, and
  refuses when the account is already bound to another `sub`.
- **Email sync.** Changing the provider email later did not change the account while "Require SSO" was off. Per the
  source, sync runs only with enforcement on, and never takes an email another account holds.
- **Group gate.** A user outside the `fluxer` group got Authentik's "Request has been denied" and never reached
  Fluxer.
- **Require SSO** (`sso_enforced`, from the source, not switched on here): refuses every local sign-in (password,
  passkey, MFA ticket) for every account, admins included. With Authentik down, nobody can sign in.

## Design changes

- **Fluxer holds do not ban.** A ban always carries a rule-violation decision and notifies the user, which is wrong
  for a parental or admin hold. A Fluxer hold instead:
  1. removes the person from Authentik's Fluxer group (hallpass already syncs this in its Authentik sync);
  2. relies on Fluxer "Require SSO", so the person cannot sign in again while outside the group;
  3. ends the person's Fluxer sessions through `DELETE /v1/admin/users/{id}/sessions` (hallmonitor).

  Restoring needs no Fluxer call: hallpass puts the person back in the group and they sign in again. Hallmonitor
  reports `suspended` once the sessions are ended and `allowed` when no hold applies; it never sets or reads the
  DISABLED bit as its own state. The key drops `user:temp_ban` and `enforcement:view`, and keeps
  `user:update_flags` only because session termination requires it.
- **Per-account failures stay per account.** An `http` or `unexpected` failure on one Fluxer account reports only that
  subject as `error`; `auth` and `unreachable` still fail every Fluxer subject, since the key or the instance is the
  cause.
- **Fluxer account lookup by SSO subject.** Hallmonitor finds a person's Fluxer account from their Authentik uuid
  with `GET /v1/admin/sso-identities?provider_id=&subject=`. The provider id is configuration (read once from any
  bound account's identity list); a null result means the person has not signed in to Fluxer yet.

## Minecraft

Only the setup note at the end is from a live server. Until `tools/live/minecraft_check.py` is run, the adapter
follows the plan's defaults, and each other item below is pending live confirmation. Hosts and names are
placeholders such as `<mc-host>`.

- **End-of-response sentinel.** A Minecraft command is sent alone. Its sentinel (type 200) follows only after the first
  response packet for that command has arrived, and never in the same read as the command, because a vanilla server
  may drop the connection when it receives both at once. The Source dialect keeps its pipelined empty sentinel.
- **Whitelist list.** Parsed from `There are N whitelisted player(s): a, b, c`, or `There are no whitelisted players`.
- **Ban list.** Each entry is read as `NAME was banned by BANNER: REASON`. A name counts as banned only at the start
  of an entry line, so a name that appears only as the banner or in the reason is not banned.
- **Kick check.** After a suspension the adapter runs `list` on the same connection, and parses
  `There are N of a max of M players online: a, b`. If a subject's name is still online, the result is
  `still_connected`, and the subject is not reported as suspended.
- **Names sent to the console.** A Java name matches 1 to 16 letters, digits or underscores. A Bedrock gamertag uses
  the same class for now, since spaces are refused until the live check shows how Floodgate passes them.
- **Setup (seen 2026-11-18).** `HOST` is a bare host name or IP, never a URL: with `https://` in front, every cycle
  failed as `unreachable` though the port was open. `PORT` is `rcon.port`, not the game port, and on Pterodactyl it
  must be one of the server's allocations. A hold does not keep an op (an account in `ops.json`) out: ops skip the
  whitelist, so a held op is kicked but can rejoin at once while the adapter reports `suspended`. Keep op status off
  accounts that hallpass may hold, and test a hold with an account that is not an op.

## Palworld

Verdict: automated (REST ban and unban, ban list read through Pterodactyl). Checked on 2026-11-15 with
`tools/live/palworld_check.py` and `tools/live/ptero_files_check.py`, using one account. Hosts and ids are placeholders
such as `<pal-host>` and `<steam-id>`.

- **REST API.** Enabled with `RESTAPIEnabled=True` and `RESTAPIPort=<port>` inside `OptionSettings` in
  `PalWorldSettings.ini`. Under Pterodactyl the port needs its own allocation on the server. The egg in use rebuilt the
  ini at startup at first, so check the values after a restart. The REST port is plain HTTP with Basic auth, so keep it
  on the LAN and reachable only from the hallmonitor host.
- **Ban list.** `Pal/Saved/SaveGames/banlist.txt`, one ban per line as `steam_<steam-id>,<PlayerUId>`, where the
  PlayerUId is 32 hexadecimal characters, followed by a newline. The adapter reads each line up to the first comma, so
  a bare `steam_<steam-id>` line is read the same way.
- **Timing.** The line was in the file on the first read after the REST ban, and gone on the first read after the REST
  unban. The 600 s forget grace window is therefore well clear.
- **Pending.** A ban for a Steam id that has never joined, which has no PlayerUId yet, was not tried. A file-edit
  fallback for when REST is down was set aside, because it is not known whether the server needs the PlayerUId in a
  line it reads.

## Pterodactyl

Panel 1.15.2, at or above the CVE-2026-54593 fix (Panel 1.12.3, Wings 1.12.2). The panel shows Wings (daemon) 1.13.4
on 2026-11-18, also at or above that fix. The `hallmonitor` subuser's Client API key (`ptlc_...`) is used with each
server's own id.

- **Permissions.** Reading a file needs `file.read` and `file.read-content`. Writing a file's contents
  (`POST /api/client/servers/<id>/files/write`) needs `file.create`, not `file.update`: the panel checks
  `file.create` on that route, and a subuser with only `file.update` got 403. So Palworld, which only reads its ban
  list, needs the two read permissions; a server whose list hallmonitor edits needs `file.create` as well. No
  `control.*` or `websocket.*` permission is needed.
- **Resources.** `GET /api/client/servers/<id>/resources` returned the state and an uptime with the read-only
  permissions.

## Valheim

Verdict: automated (permitted list through Pterodactyl, live reload). Checked on 2026-11-16 on a server run without
`-crossplay`, using one test account. Ids are placeholders such as `<steam-id>`.

- **Files.** `.config/unity3d/IronGate/Valheim/permittedlist.txt` and `bannedlist.txt`, relative to the server root.
  Both are ASCII, with no BOM and LF line endings, and each starts with a `// ...` comment line.
- **Enforcement.** With one valid entry, the permitted list is enforced: a made-up id, bare or as `Steam_<id>`, was
  refused.
- **Forms.** The test account's id was let in as bare digits, as `Steam_<steam-id>` and as `V_<steam-id>`. All three
  let the player in. The adapter writes the `V_` form, so the line keeps working if crossplay is turned on later.
- **Live reload.** Removing the test account's line while the server ran kicked the connected player. Adding it back, as
  `Steam_<steam-id>`, let them join with no restart. So `live_reload` is true for this server.
- **Not tested.** An empty permitted list (V4). Valheim is widely documented to let anyone join when the list is empty,
  so the adapter's empty-list guard (`would_open_server`) is required regardless.
- **Permissions.** Writing needs `file.create`, as in `## Pterodactyl`.

## The Ship

Verdict: automated (console commands through Pterodactyl, ban file read back). Checked on 2026-11-18 on the test server,
with crossplay off and one test account. Ids are placeholders such as `STEAM_0:<y>:<z>`, and the game directory is
`<game-dir>`.

- **Ids.** `status` shows each player as `STEAM_0:<y>:<z>` (Steam-validated), so the universe digit is 0.
- **Permanent bans.** `banid 0 STEAM_0:<y>:<z>` places a permanent ban, and `listid` shows it as `permanent`. A
  non-zero duration is a timed ban, which `writeid` does not save, so the adapter sends none.
- **Kick.** `banid 0 STEAM_0:<y>:<z> kick` does not kick a player given a Steam id. `kickid STEAM_0:<y>:<z>` does, so a
  suspension sends `banid`, `kickid` and `writeid` in that order.
- **Ban file.** `writeid` writes `<game-dir>/cfg/banned_user.cfg`, relative to the server root, with one
  `banid 0 STEAM_0:<y>:<z>` line per ban and nothing else. Only `writeid` writes it.
- **Refused on reconnect.** A banned player who reconnects is refused with a ban message. `removeid STEAM_0:<y>:<z>`
  followed by `writeid` lifts the ban and removes its line.
- **Startup.** The server loads the file at startup only if `server.cfg` has `exec banned_user.cfg`. Without that line,
  after a restart the file still held the ban, `listid` was empty and the ban was not enforced. The test server's
  `server.cfg` has the line. Hallmonitor sends no `exec` of its own, so the operator must add it.
- **Recorded.** On 2026-11-18, `POST /api/client/servers/<id>/command` answered 204 for the hallmonitor subuser, with
  `control.console`, `file.read` and `file.read-content`, and without `file.create`. A non-204 answer would show as an
  `http` error, and no ban would be recorded as ours.

## Abiotic Factor

Verdict: manual for now (adapter deferred). Researched on 2026-11-18. Holds are applied by hand in the in-game admin
menu, and the hallpass service uses adapter `manual`, not automatically suspendable.

- **Admin file.** `AbioticFactor/Saved/SaveGames/Server/Admin.ini` under the server root, moved by `-AdminIniPath=`
  (relative to `Saved/`). The server under test has a `[BannedPlayers]` section in it.
- **Documented.** The official dedicated-server wiki covers `[Moderators]` with `Moderator=<steam-id>` lines (Steam
  only), says to close the server before editing, and documents `-AdminPassword=` for in-game admin by password. It
  does not mention `[BannedPlayers]`.
- **Reported by hosting guides, not confirmed here.** A ban is a `BannedPlayer=<steam-id>` line under
  `[BannedPlayers]`, and deleting the line lifts it. Edits are made with the server stopped. There are no console or
  RCON moderation commands, and there is no allowlist.
- **Before automating.** Check live the exact line an in-game ban writes, the file's encoding and line endings,
  whether a hand edit takes effect at once or only after a restart, and whether a running server rewrites the file
  on save or shutdown. A file-based ban list would need its own records, so that it lifts only the bans it placed.
