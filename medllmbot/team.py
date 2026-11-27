"""Who works with clients in Telegram: the owner from ADMIN_ID and the staff from the admin panel.

The bot tells staff from clients only by the Telegram user id, which Telegram itself puts into every update:
a name, photo or username can be copied by anyone, so they are never trusted.

- The owner gets everything: leads, operator calls, suspicious-message and spending-limit alerts.
- Staff («Telegram staff» in the admin panel) get operator calls and leads and answer clients as the operator.
- A client who calls the operator is offered to every operator; the first one to reply takes the dialog
  (`clients.operator_chat_id`), the others are told who took it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from medllmbot.i18n import N_, Gettext

OWNER = N_("the owner")


@dataclass(frozen=True)
class Team:
    owner_id: int  # ADMIN_ID: a private chat, or a group that gets notifications but cannot reply
    staff: Mapping[int, str]  # Telegram user id → name, active staff only

    def is_member(self, user_id: int) -> bool:
        return user_id == self.owner_id or user_id in self.staff

    @property
    def recipients(self) -> list[int]:
        """Who gets leads: the owner first, then the staff."""
        return [self.owner_id, *(chat_id for chat_id in self.staff if chat_id != self.owner_id)]

    @property
    def operators(self) -> list[int]:
        """Who gets operator calls and can answer clients: private chats only, a group cannot reply in a name."""
        return [chat_id for chat_id in self.recipients if chat_id > 0]

    def holder(self, operator_chat_id: int | None) -> int | None:
        """The operator who took the dialog, if they are still on the team; None — the dialog is free.

        A staff member turned off in the admin panel no longer gets the client's messages.
        """
        return operator_chat_id if operator_chat_id is not None and operator_chat_id in self.operators else None

    def name_of(self, user_id: int, _: Gettext) -> str:
        """How the other operators see this one: the name from the admin panel, or «the owner»."""
        return self.staff.get(user_id) or _(OWNER)
