from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, default="")
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    camera_permissions: Mapped[list["CameraPermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profiles: Mapped[list["UserProfile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    device_type: Mapped[str] = mapped_column(String(30), default="hikvision_dvr", nullable=False)
    rtsp_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    host: Mapped[str] = mapped_column(String(120), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    rtsp_port: Mapped[int] = mapped_column(Integer, default=554, nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    subtype: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    isapi_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rtsp_path_template: Mapped[str] = mapped_column(
        String(255),
        default="/cam/realmonitor?channel={channel}&subtype={subtype}",
        nullable=False,
    )
    snapshot_path: Mapped[str] = mapped_column(
        String(255),
        default="/ISAPI/Streaming/channels/{stream_id}/picture",
        nullable=False,
    )
    snapshot_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user_permissions: Mapped[list["CameraPermission"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )


class CameraPermission(Base):
    __tablename__ = "camera_permissions"
    __table_args__ = (UniqueConstraint("user_id", "camera_id", name="uq_user_camera"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    can_view_live: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_view_snapshot: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="camera_permissions")
    camera: Mapped[Camera] = relationship(back_populates="user_permissions")


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    users: Mapped[list["UserProfile"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    camera_permissions: Mapped[list["ProfileCameraPermission"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (UniqueConstraint("user_id", "profile_id", name="uq_user_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)

    user: Mapped[User] = relationship(back_populates="profiles")
    profile: Mapped[Profile] = relationship(back_populates="users")


class UserCameraLayout(Base):
    __tablename__ = "user_camera_layouts"
    __table_args__ = (UniqueConstraint("user_id", "camera_id", name="uq_user_camera_layout"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProfileCameraPermission(Base):
    __tablename__ = "profile_camera_permissions"
    __table_args__ = (UniqueConstraint("profile_id", "camera_id", name="uq_profile_camera"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    can_view_live: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_view_snapshot: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    profile: Mapped[Profile] = relationship(back_populates="camera_permissions")
    camera: Mapped[Camera] = relationship()
