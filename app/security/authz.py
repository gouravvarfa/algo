"""Roles and permissions. Roles are cumulative: developer ⊇ admin ⊇ user. Permissions are named strings, roles are bundles of them,
so a new role (e.g. 'support') is a new bundle and no route changes. Every check runs on the server."""
from __future__ import annotations

ROLES = ("user", "admin", "developer")
RANK = {"user": 0, "admin": 1, "developer": 2}

_USER = frozenset({"session.self"})
_ADMIN = _USER | {"users.read", "users.create", "users.manage", "audit.read"}
_DEVELOPER = _ADMIN | {"system.read", "system.manage"}
PERMISSIONS = {"user": _USER, "admin": _ADMIN, "developer": _DEVELOPER}


def has(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())


def permissions(role: str) -> list[str]:
    return sorted(PERMISSIONS.get(role, frozenset()))


def is_owner(user) -> bool:
    """The owner is the first developer (made by scripts/create_owner.py, so created_by is empty). Only the owner creates admins/developers."""
    return user["role"] == "developer" and user["created_by"] is None


def can_create_role(actor, role: str) -> bool:
    if role == "user":
        return has(actor["role"], "users.create")
    return is_owner(actor)


def can_manage(actor, target) -> bool:
    """Disable / delete / force-logout / reset a user: never yourself, and only someone ranked above them (the owner may manage anyone)."""
    if actor["id"] == target["id"]:
        return False
    return is_owner(actor) or RANK[actor["role"]] > RANK[target["role"]]
