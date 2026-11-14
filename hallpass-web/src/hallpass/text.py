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
TYPE_CHANGE_REFUSED_IS_PARENT = "This person has linked children, so they cannot be changed to type child."
TYPE_CHANGE_REFUSED_HAS_PARENTS = "This child has linked parents. Unlink them before changing the type."
NO_FLUXER_ADDRESS_FLAG = ("No Fluxer address: this child has no email, and no linked parent has one. "
                          "The child cannot sign in to Fluxer until one is set.")
CSRF_FAILED = "The form has expired or came from another site. Reload the page and try again."
SIGNIN_UNAVAILABLE = "Sign-in is unavailable right now. Try again in a few minutes."
SIGN_IN_FAILED = "Sign-in did not complete. Start again from the sign-in link."
NOT_FOUND = "That page does not exist."
INVALID_INPUT = "Check the values and try again."
IDENTITY_TAKEN = "That identity is already recorded for this service."
GROUP_TAKEN = "A group with that name already exists."
ENTITLEMENT_DENY_NOT_PERSON = ("A deny applies only to one person. For an account type or a group, "
                               "remove the grant instead.")
AUTHENTIK_NOT_UPDATED = "The change is saved in hallpass, but Authentik could not be updated yet."
INVITATION_NOT_CREATED = "The invitation was not created because Authentik did not respond. Create it again below."
TERMS_NOT_PUBLISHED = "No terms have been published yet. Hallpass cannot be used until the administrator publishes them."
TERMS_CHANGED = "The terms changed while this page was open. Read the current version and accept it."
TERMS_NOT_NOW = "Not now"
# One message per grants.GrantRefused reason.
GRANT_REFUSED = {
    "not_linked_parent": "Only a parent linked to this child can approve them.",
    "parent_terms": "You must accept the current terms before you can approve a child.",
    "parent_inactive": "Your account must be active before you can approve a child.",
    "child_not_awaiting": "This child is not waiting for approval.",
}
# The personal page and the service rows, shared with a parent's view of a child.
STATUS_ALLOWED = "Allowed"
STATUS_SUSPENDED = "Suspended"
CAN_AUTO_SUSPEND = "can be automatically suspended"
CANNOT_AUTO_SUSPEND = "cannot be automatically suspended"
NO_GAME_IDENTITY = "no game identity on file"
# One message per access.Access reason, in the order access.evaluate adds them.
REASON_RETIRED = "The service has been retired."
REASON_NOT_ENTITLED = "You are not entitled to this service."
REASON_NOT_ACTIVE = "The account is not active yet. A parent must approve it."
REASON_TERMS = "The current terms must be accepted first."
FEED_SHOWN_ONCE = ("This feed link is shown only once. Copy it now: reloading the page does not show it again. "
                   "Generating a new link revokes this one.")
FEED_NO_ADDRESS = "This feed has no address set, so no feed link can be made. Ask the administrator."
# The admin service pages.
SERVICE_TAKEN = "A service with that slug already exists."
ENTITLEMENT_TAKEN = "That grant or deny is already set for this service."
