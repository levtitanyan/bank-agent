"""Which tariff fields are extracted together.

One call per field would bind evidence most tightly but costs ten calls per
product, which matters on a free-tier quota and in a live demonstration. Fields
are therefore grouped by what a passage tends to state at once - a rate table
gives both rates, a fee schedule gives all three fees - and each group's prompt
contains only the chunks retrieved for *its own* fields, so evidence stays bound
to the passages that justified it.

A field that fails inside a group is retried alone, which keeps the cost saving
without making a group failure contagious.
"""

from __future__ import annotations

from typing import Final

FIELD_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("rates", ("nominal_rate", "effective_rate")),
    ("fees", ("application_fee", "disbursement_fee", "service_fee")),
    ("terms", ("currency", "term", "amount")),
    ("other", ("collateral", "salary_privileges")),
)
"""Group name to field ids. Four calls per product instead of ten."""


def group_of(field_id: str) -> str:
    """Return the group a field belongs to.

    Args:
        field_id: Registry field id.

    Returns:
        The group name.

    Raises:
        KeyError: If the field is in no group, which means the registry grew
            and this module was not updated.
    """
    for name, members in FIELD_GROUPS:
        if field_id in members:
            return name
    raise KeyError(f"{field_id} belongs to no extraction group")
