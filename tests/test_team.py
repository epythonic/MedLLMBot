"""Who works with clients in Telegram: the owner from ADMIN_ID and the staff from the admin panel."""

from medllmbot.i18n import gettext_for
from medllmbot.team import Team

OWNER = 777
ANNA = 555
BORIS = 556
RU = gettext_for("ru")


def test_members_are_known_by_id_only() -> None:
    team = Team(OWNER, {ANNA: "Анна"})
    assert team.is_member(OWNER)
    assert team.is_member(ANNA)
    assert not team.is_member(42)


def test_owner_comes_first_and_is_not_repeated() -> None:
    # The owner added to the staff list as well still gets one notification
    team = Team(OWNER, {ANNA: "Анна", OWNER: "Владелец", BORIS: "Борис"})
    assert team.recipients == [OWNER, ANNA, BORIS]
    assert team.operators == [OWNER, ANNA, BORIS]


def test_group_owner_gets_leads_but_is_not_an_operator() -> None:
    # A group cannot reply in a person's name: the operator works through the staff only
    team = Team(-100123, {ANNA: "Анна"})
    assert team.recipients == [-100123, ANNA]
    assert team.operators == [ANNA]
    assert Team(-100123, {}).operators == []


def test_holder_must_still_be_an_operator() -> None:
    team = Team(OWNER, {ANNA: "Анна"})
    assert team.holder(ANNA) == ANNA
    assert team.holder(OWNER) == OWNER
    assert team.holder(BORIS) is None  # turned off or deleted: the dialog is free again
    assert team.holder(None) is None


def test_names() -> None:
    team = Team(OWNER, {ANNA: "Анна"})
    assert team.name_of(ANNA, RU) == "Анна"
    assert team.name_of(OWNER, RU) == "владелец"
    assert team.name_of(OWNER, gettext_for("en")) == "the owner"
