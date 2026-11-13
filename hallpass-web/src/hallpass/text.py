# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-facing strings for hallpass pages and refusals. Plain wording; no names, addresses or ids in any of them."""

NOT_REGISTERED = "This account is not registered with hallpass. Ask the administrator for an invitation."
LOCKED = "This account is locked. Ask the administrator to unlock it."
FORBIDDEN = "You do not have permission to do that."
INVITE_SHOWN_ONCE_HINT = ("This link works once. If it has already been used, or it is lost, "
                          "the administrator can make a new one.")
LINK_REFUSED_CHILD_TYPE = "Only a person of type child can be linked as a child."
LINK_REFUSED_PARENT_TYPE = "A child cannot be linked as a parent."
NO_FLUXER_ADDRESS_FLAG = ("No Fluxer address: this child has no email, and no linked parent has one. "
                          "The child cannot sign in to Fluxer until one is set.")
CSRF_FAILED = "The form has expired or came from another site. Reload the page and try again."
