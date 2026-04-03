from sqlalchemy import text

from app.core.database import async_session
from app.models.schemas import RoleType, DomainType, RoleContext, ResolvedRoles, ROLE_DOMAIN_MAP

# In-memory cache — roles rarely change
_role_cache: dict[RoleType, RoleContext] = {}

DEFAULT_ROLE = RoleType.cybersecurity_executive


async def load_roles() -> None:
    """Load all roles from identity_memory into cache at startup."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT role, domain, description, goals, preferences, constraints FROM identity_memory")
        )
        for row in result.mappings():
            try:
                role_type = RoleType(row["role"])
            except ValueError:
                continue
            _role_cache[role_type] = RoleContext(
                role=role_type,
                domain=DomainType(row["domain"]),
                description=row["description"] or "",
                goals=row["goals"],
                preferences=row["preferences"],
                constraints=row["constraints"],
            )


def _get_role(role: RoleType) -> RoleContext:
    """Get a role from cache, falling back to an empty context."""
    if role in _role_cache:
        return _role_cache[role]
    return RoleContext(
        role=role,
        domain=ROLE_DOMAIN_MAP.get(role, DomainType.professional),
    )


async def resolve_roles(
    primary: RoleType | None,
    secondary: RoleType | None = None,
) -> ResolvedRoles:
    """Resolve primary + optional secondary role (max 2 active per spec)."""
    if not _role_cache:
        await load_roles()

    primary_ctx = _get_role(primary or DEFAULT_ROLE)

    secondary_ctx = None
    if secondary and secondary != primary:
        secondary_ctx = _get_role(secondary)

    return ResolvedRoles(primary=primary_ctx, secondary=secondary_ctx)


def get_all_roles() -> list[RoleContext]:
    """Return all cached roles."""
    return list(_role_cache.values())
