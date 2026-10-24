"""Reference model of read authorization: the executable definition behind ACC-1.

Deliberately written apart from `authz.readable_collections` and `Principal`: tests compare
the two, so a bug has to be made twice, the same way, to slip through.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HIERARCHY = ["Reader", "Contributor", "Curator", "Admin"]
CLEARANCE_PREFIX = "Clearance."


@dataclass(frozen=True)
class Actor:
    subject: str
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()

    @property
    def clearances(self) -> frozenset[str]:
        held = {r[len(CLEARANCE_PREFIX) :] for r in self.roles if r.startswith(CLEARANCE_PREFIX)}
        return frozenset(held | {"UNCLASSIFIED"})

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles

    def granted_roles(self) -> frozenset[str]:
        """Roles a grant can match: those held, plus every hierarchy role below the highest.

        Clearance roles are not access roles: a grant to `Clearance.CUI` matches nobody.
        """
        held = {r for r in self.roles if not r.startswith(CLEARANCE_PREFIX)}
        ranks = [HIERARCHY.index(r) for r in held if r in HIERARCHY]
        below = HIERARCHY[: max(ranks) + 1] if ranks else []
        return frozenset(held | set(below))


@dataclass(frozen=True)
class GrantKey:
    principal_type: str
    principal: str


@dataclass
class World:
    """What the model knows about the catalog: markings, collections and their grants."""

    markings: dict[str, int] = field(default_factory=dict)  # code -> rank
    collections: dict[str, str] = field(default_factory=dict)  # id -> marking code
    grants: dict[str, dict[GrantKey, str]] = field(default_factory=dict)  # id -> key -> access

    def clearance_rank(self, actor: Actor) -> int:
        ranks = [self.markings[c] for c in actor.clearances if c in self.markings]
        return max(ranks, default=0)

    def cleared_for(self, actor: Actor, marking: str) -> bool:
        return self.markings[marking] <= self.clearance_rank(actor)

    def has_grant(self, actor: Actor, collection_id: str) -> bool:
        roles = actor.granted_roles()
        for key in self.grants.get(collection_id, {}):
            if (
                (key.principal_type == "role" and key.principal in roles)
                or (key.principal_type == "group" and key.principal in actor.groups)
                or (key.principal_type == "user" and key.principal == actor.subject)
            ):
                return True
        return False

    def can_read(self, actor: Actor, collection_id: str) -> bool:
        marking = self.collections.get(collection_id)
        if marking is None or not self.cleared_for(actor, marking):
            return False
        return actor.is_admin or self.has_grant(actor, collection_id)

    def readable(self, actor: Actor) -> set[str]:
        return {c for c in self.collections if self.can_read(actor, c)}

    @classmethod
    def from_db(cls, conn) -> World:
        world = cls()
        for code, rank in conn.execute("SELECT code, rank FROM authz.markings"):
            world.markings[code] = rank
        for cid, marking in conn.execute("SELECT id, marking FROM catalog.collections"):
            world.collections[cid] = marking
            world.grants[cid] = {}
        for cid, ptype, principal, access in conn.execute(
            "SELECT collection_id, principal_type, principal, access FROM authz.collection_grants"
        ):
            world.grants[cid][GrantKey(ptype, principal)] = access
        return world
