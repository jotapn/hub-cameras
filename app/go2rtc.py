from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Camera
from app.services import build_rtsp_url


GO2RTC_BASE_URL = os.getenv("GO2RTC_BASE_URL", "http://127.0.0.1:1984").rstrip("/")
GO2RTC_CONFIG_PATH = Path(os.getenv("GO2RTC_CONFIG_PATH", "go2rtc.yaml"))
GO2RTC_WEB_PORT = int(os.getenv("GO2RTC_WEB_PORT", "1984"))
GO2RTC_LISTEN_HOST = os.getenv("GO2RTC_LISTEN_HOST", "0.0.0.0")


def camera_stream_name(camera: Camera) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", camera.name.lower()).strip("-")
    slug = slug or f"camera-{camera.id}"
    return f"cam-{camera.id}-{slug[:40]}".strip("-")


def build_go2rtc_streams(cameras: list[Camera]) -> dict[str, str]:
    streams: dict[str, str] = {}
    for camera in cameras:
        streams[camera_stream_name(camera)] = build_rtsp_url(camera)
    return streams


def build_go2rtc_config(cameras: list[Camera]) -> dict:
    return {
        "api": {"listen": f"{GO2RTC_LISTEN_HOST}:{GO2RTC_WEB_PORT}"},
        "rtsp": {"listen": f"{GO2RTC_LISTEN_HOST}:8554"},
        "webrtc": {"listen": ":8555"},
        "streams": build_go2rtc_streams(cameras),
    }


def sync_go2rtc_config(db: Session) -> Path:
    cameras = db.scalars(select(Camera).order_by(Camera.id)).all()
    config = build_go2rtc_config(cameras)
    GO2RTC_CONFIG_PATH.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return GO2RTC_CONFIG_PATH


def go2rtc_api_available(timeout: int = 3) -> bool:
    try:
        response = requests.get(f"{GO2RTC_BASE_URL}/api/streams", timeout=timeout)
        response.raise_for_status()
        return True
    except Exception:
        return False


def upsert_go2rtc_stream(camera: Camera, timeout: int = 5) -> bool:
    try:
        response = requests.put(
            f"{GO2RTC_BASE_URL}/api/streams",
            params={"name": camera_stream_name(camera), "src": build_rtsp_url(camera)},
            timeout=timeout,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def delete_go2rtc_stream(camera: Camera, timeout: int = 5) -> bool:
    try:
        response = requests.delete(
            f"{GO2RTC_BASE_URL}/api/streams",
            params={"name": camera_stream_name(camera)},
            timeout=timeout,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def sync_go2rtc_api(db: Session) -> bool:
    cameras = db.scalars(select(Camera).order_by(Camera.id)).all()
    if not go2rtc_api_available():
        return False
    ok = True
    for camera in cameras:
        ok = upsert_go2rtc_stream(camera) and ok
    return ok


def build_public_go2rtc_base_url(request_scheme: str, request_host: str) -> str:
    parsed = urlsplit(GO2RTC_BASE_URL)
    scheme = parsed.scheme or request_scheme or "http"
    host = request_host.split(":", 1)[0]
    return f"{scheme}://{host}:{GO2RTC_WEB_PORT}"
