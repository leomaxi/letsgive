from fastapi import HTTPException, status

from app.db.models.membership import Membership, Role


class TenantContext:
    """Resolved identity + role for a single organization-scoped request."""

    def __init__(self, user_id: str, organization_id: str, role: Role, membership_id: str):
        self.user_id = user_id
        self.organization_id = organization_id
        self.role = role
        self.membership_id = membership_id


def require_roles(membership: Membership, *allowed: Role) -> None:
    if membership.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{membership.role.value}' is not permitted to perform this action.",
        )
