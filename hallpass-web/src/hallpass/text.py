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
# A child reads the terms but never accepts them. A linked parent accepts for the child.
CHILD_CANNOT_ACCEPT = "A child does not accept the terms. A parent accepts them for you."
TERMS_CHILD_COVERED = "A parent accepts these terms for you."
TERMS_CHILD_NOT_COVERED = "No parent has accepted these terms yet, so your services stay off until one does."
# The parent's approval text. The child's name and the version are filled in by the page.
GRANT_ASK = ("Approving lets {child} use the services. You confirm that you accept terms version {version}, "
             "on the child's behalf.")
GRANT_APPROVED_WAITING = "This child is approved. They become active when they sign in."
# One message per grants.GrantRefused reason.
GRANT_REFUSED = {
    "not_linked_parent": "Only a parent linked to this child can approve them.",
    "parent_terms": "You must accept the current terms before you can approve a child.",
    "parent_inactive": "Your account must be active before you can approve a child.",
    "child_not_awaiting": "This child is not waiting for approval.",
    "already_granted": "This child is already approved.",
}
# The personal page and the service rows, shared with a parent's view of a child.
STATUS_ALLOWED = "Allowed"
STATUS_SUSPENDED = "Suspended"
CAN_AUTO_SUSPEND = "can be automatically suspended"
CANNOT_AUTO_SUSPEND = "cannot be automatically suspended"
NO_GAME_IDENTITY = "no game identity on file"
MANUALLY_SUSPENDED = "manually suspended"
# One message per access.Access reason, in the order access.evaluate adds them.
REASON_RETIRED = "The service has been retired."
REASON_NOT_ENTITLED = "Not entitled to this service."
REASON_NOT_ACTIVE = "The account is not active yet. A parent must approve it."
REASON_TERMS = "The current terms must be accepted first."
# A child cannot accept the terms, so their reason names the parent who must.
REASON_TERMS_CHILD = "A parent must accept the current terms first."
REASON_HELD = "A hold is in place."
FEED_SHOWN_ONCE = ("This feed link is shown only once. Copy it now: reloading the page does not show it again. "
                   "Generating a new link revokes this one.")
FEED_NO_ADDRESS = "This feed has no address set, so no feed link can be made. Ask the administrator."
# The admin service pages.
SERVICE_TAKEN = "A service with that slug already exists."
SERVICE_SLUG_INVALID = ("A slug is 1 to 64 lower-case letters and digits, with single hyphens only between them. "
                        "It starts and ends with a letter or digit. Underscores are not allowed, and neither are "
                        "doubled hyphens, because hallmonitor cannot configure a slug that has them.")
SERVICE_SUSPEND_NEEDS_ADAPTER = ("A service that can be suspended automatically needs an adapter that hallmonitor runs. "
                                 "Choose an adapter, or untick the box for a service that is suspended by hand.")
# Shown beside the retire button and the identity section: hallmonitor stops managing the pair, and leaves its last
# applied state in place.
SERVICE_RETIRE_NOTE = ("Retiring a service stops hallmonitor from managing it, and it leaves the last state it applied in "
                       "place. Before you retire a game service, lift its holds first, and clean up the game server by "
                       "hand if it needs it: a whitelisted player stays whitelisted, and an own ban stays placed.")
IDENTITY_REMOVE_NOTE = ("Removing an identity stops hallmonitor from managing this person on that service, and it leaves "
                        "the last state it applied in place. Before you remove a game identity, lift the holds on that "
                        "service first, and clean up the game server by hand if it needs it.")
# A shared identity (several people hold the same id on one service). Members and parents are not told who the other
# holder is; the administrator's page names the holders who caused the pause.
SHARED_IDENTITY_PAUSED = ("Someone who shares one of your ids on this service can't use it right now, so that id is "
                          "paused for everyone who uses it.")
SHARED_PAUSE_CAUSES = "Paused because of the suspension of"
IDENTITY_ALSO_HELD = "Also held by"
ENTITLEMENT_TAKEN = "That grant or deny is already set for this service."
# Holds and the conflict rules. Actor labels are what a person sees as "Suspended by ...".
ACTOR_ADMIN = "the administrator"
ACTOR_SYSTEM = "the system"
STRICT_NOT_YOURS = "{names} placed a hold on this service. In strict mode only the parent who placed a hold can lift it."
RESTORE_NOT_AUTOMATIC = "This service cannot be restored automatically. Ask the administrator."
NOT_YOUR_CHILD = "You can only change holds for a child linked to you."
NOT_CHILD_SERVICE = "This child does not have that service, so a hold cannot be placed on it."
LOCKDOWN_ADMIN_ONLY = ("Only the administrator can lift an administrator hold or a lockdown. "
                       "A lockdown is lifted by resolving its review.")
MARK_NO_HOLD = "Only a held service can be marked manually suspended."
MARK_NOT_MANUAL = "Only a service that cannot be automatically suspended can be marked manually suspended."
# Notification emails are plain text. The child's display name and the actor label are the only names in them.
CHANGE_EMAIL_SUBJECT = "Hallpass: access changed for {child}"
CHANGE_EMAIL_BODY = ("{actor} changed the access of {child} in hallpass.\n\n"
                     "Sign in to hallpass for details.\n")
LOCKDOWN_EMAIL_SUBJECT = "Hallpass: access locked down for {child}"
LOCKDOWN_EMAIL_BODY_DEFAULT = ("Access for {child} is suspended on every service, because two changes by different "
                               "parents were made within the review window. The administrator will review it.\n\n"
                               "Sign in to hallpass for details.\n")
LOCKDOWN_EMAIL_BODY_MONITORED = ("Access for {child} is suspended on every service, because two changes by different "
                                 "parents were made within the review window. The agreement note reads:\n\n"
                                 "{agreement_note}\n\nThe administrator will review it. "
                                 "Sign in to hallpass for details.\n")
LOCKDOWN_REVIEW_REASON_DEFAULT = "Two changes by different parents within the review window."
LOCKDOWN_REVIEW_REASON_MONITORED = ("Two changes by different parents within the review window. "
                                    "The agreement note reads: {agreement_note}")
# Monitor reports, shown next to the access status: what hallmonitor last reported for a service row.
NOT_YET_CONFIRMED = "requested, not yet confirmed"
BANNED_IN_GAME = "banned in game"
SERVICE_ERROR = "the service could not be reached"
NOT_CONFIGURED = "not configured in hallmonitor"
PENDING_RESTART = "applied, waiting for the service to restart"
