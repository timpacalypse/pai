"""Simple user management — first-name login, no passwords."""

import logging
from sqlalchemy import text
from app.core.database import async_session

logger = logging.getLogger("pai.services.user")


async def login_or_create(first_name: str) -> dict:
    """Log in by first name. Creates the user if they don't exist."""
    name = first_name.strip()
    if not name:
        return {"error": "Name is required"}

    async with async_session() as session:
        # Try to find existing user (case-insensitive)
        result = await session.execute(
            text(
                "SELECT id, first_name, created_at, last_login_at "
                "FROM pai_users WHERE LOWER(first_name) = LOWER(:name)"
            ),
            {"name": name},
        )
        row = result.mappings().fetchone()

        if row:
            # Update last login
            await session.execute(
                text("UPDATE pai_users SET last_login_at = NOW() WHERE id = :id"),
                {"id": row["id"]},
            )
            await session.commit()
            return {
                "id": row["id"],
                "first_name": row["first_name"],
                "created": False,
            }
        else:
            # Create new user
            result = await session.execute(
                text(
                    "INSERT INTO pai_users (first_name) VALUES (:name) "
                    "RETURNING id, first_name"
                ),
                {"name": name},
            )
            new_row = result.mappings().fetchone()
            await session.commit()
            logger.info("user_created name=%s id=%d", name, new_row["id"])
            return {
                "id": new_row["id"],
                "first_name": new_row["first_name"],
                "created": True,
            }


async def get_user(user_id: int) -> dict | None:
    """Get user by ID."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, first_name FROM pai_users WHERE id = :id"),
            {"id": user_id},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    """List all users."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, first_name, last_login_at FROM pai_users ORDER BY last_login_at DESC")
        )
        return [dict(r) for r in result.mappings()]
