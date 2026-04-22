from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.security import hash_password


def seed_admin(db: Session) -> None:
    admin_exists = db.scalar(select(User).where(User.username == "admin"))
    if admin_exists:
        return

    db.add(
        User(
            username="admin",
            full_name="Administrador",
            password_hash=hash_password("admin123"),
            is_admin=True,
            is_active=True,
        )
    )
    db.commit()
