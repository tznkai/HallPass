# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Source console behind the Pterodactyl command route, and the ban file it writes.

The console keeps its ban list in memory, as the live server did: `banid 0 <id>` and `removeid <id>` change it at once,
`kickid <id>` changes nothing, and `writeid` replaces the ban file with one `banid 0 <id>` line per ban, and nothing
else. The adapter reads the file back through Pterodactyl, so until `writeid` runs the file still shows the old list.
Any other command is accepted and does nothing, as the real console answers and the adapter never sends one.

Attach `SourceConsole.run` to a FakePteroState as its `console`. Every test calls it through the Pterodactyl route.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.fake_ptero_files import FakePteroState


def file_text(listed: list[str] | tuple[str, ...]) -> str:
    """The ban file the console writes for a list: one `banid 0 <id>` line per id, and nothing else."""
    return "".join(f"banid 0 {sid}\n" for sid in listed)


@dataclass
class SourceConsole:
    server: str
    banlist_path: str
    listed: list[str] = field(default_factory=list)   # the ids `listid` would show, updated at once
    keep_on_removeid: bool = False                     # a server that answers removeid and never drops the id
    ignore_banid: bool = False                         # a server that answers banid and never adds the id

    def run(self, state: FakePteroState, text: str) -> None:
        verb, _, args = text.partition(" ")
        fields = args.split()
        if verb == "banid" and len(fields) == 2 and fields[0] == "0":
            if not self.ignore_banid and fields[1] not in self.listed:
                self.listed.append(fields[1])
        elif verb == "removeid" and len(fields) == 1:
            if not self.keep_on_removeid and fields[0] in self.listed:
                self.listed.remove(fields[0])
        elif text == "writeid":
            state.files[(self.server, self.banlist_path)] = file_text(self.listed)
