"""Hypothesis strategies shared by the property tests and the simulator."""

from hypothesis import strategies as st

from tests.model import HIERARCHY, Actor, GrantKey

ROLES = [*HIERARCHY, "Clearance.CUI", "Analyst"]
GROUPS = ["g-alpha", "g-bravo"]
SUBJECTS = [f"s-{n}" for n in range(4)]

# Grantee names deliberately overlap across kinds (a group called "Admin", a user called
# "g-alpha", a role grant to "Clearance.CUI"): a grant must match on its own kind only, and
# disjoint names would hide that.
NAMES = [*ROLES, *GROUPS, *SUBJECTS]

grant_keys = st.builds(GrantKey, st.sampled_from(["role", "group", "user"]), st.sampled_from(NAMES))

actors = st.builds(
    Actor,
    subject=st.sampled_from(SUBJECTS),
    roles=st.frozensets(st.sampled_from(ROLES), max_size=3),
    groups=st.frozensets(st.sampled_from(GROUPS), max_size=2),
)
