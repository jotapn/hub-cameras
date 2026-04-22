from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import quote, urlsplit

import requests
from requests.auth import HTTPDigestAuth
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Camera, CameraPermission, ProfileCameraPermission, User, UserCameraLayout, UserProfile


def build_stream_id(channel: int, subtype: int) -> str:
    return f"{channel}0{subtype + 1}"


def build_http_base_url(camera: Camera) -> str:
    scheme = "https" if getattr(camera, "use_ssl", False) else "http"
    return f"{scheme}://{camera.host}:{camera.port}"


def build_rtsp_url(camera: Camera) -> str:
    if camera.rtsp_url:
        return camera.rtsp_url
    if camera.device_type in {"hikvision_dvr", "hikvision_nvr"}:
        stream_id = build_stream_id(camera.channel, camera.subtype)
        path = f"/Streaming/Channels/{stream_id}"
    else:
        path = camera.rtsp_path_template.format(channel=camera.channel, subtype=camera.subtype)
    username = quote(camera.username, safe="")
    password = quote(camera.password, safe="")
    return f"rtsp://{username}:{password}@{camera.host}:{camera.rtsp_port}{path}"


def build_snapshot_url(camera: Camera) -> str:
    if camera.snapshot_url:
        return camera.snapshot_url
    stream_id = build_stream_id(camera.channel, camera.subtype)
    path = camera.snapshot_path.format(
        channel=camera.channel,
        subtype=camera.subtype,
        stream_id=stream_id,
    )
    return f"{build_http_base_url(camera)}{path}"


def fetch_camera_snapshot(camera: Camera, timeout: int = 10) -> requests.Response:
    auth = None
    if camera.username or camera.password:
        auth = HTTPDigestAuth(camera.username, camera.password)
    response = requests.get(build_snapshot_url(camera), auth=auth, timeout=timeout, verify=camera.verify_ssl)
    response.raise_for_status()
    return response


def fetch_isapi_info(camera: Camera, timeout: int = 10) -> dict[str, str]:
    if not camera.isapi_enabled or camera.device_type == "generic_rtsp":
        return {"status": "disabled", "detail": "ISAPI desabilitada para esta camera."}

    endpoints = {
        "device_info": f"{build_http_base_url(camera)}/ISAPI/System/deviceInfo",
        "channels": f"{build_http_base_url(camera)}/ISAPI/Streaming/channels",
    }
    results: dict[str, str] = {}
    for key, url in endpoints.items():
        try:
            response = requests.get(
                url,
                auth=HTTPDigestAuth(camera.username, camera.password),
                timeout=timeout,
                verify=camera.verify_ssl,
            )
            response.raise_for_status()
            results[key] = response.text[:4000]
        except Exception as exc:
            results[key] = f"Erro: {exc}"
    return results


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _read_rtsp_response(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\r\n\r\n" in b"".join(chunks):
            break
    return b"".join(chunks).decode("utf-8", errors="ignore")


def _parse_auth_header(response_text: str) -> tuple[str | None, dict[str, str]]:
    match = re.search(r"WWW-Authenticate:\s*([^\r\n]+)", response_text, re.IGNORECASE)
    if not match:
        return None, {}
    header_value = match.group(1).strip()
    if header_value.lower().startswith("basic"):
        return "basic", {}
    if not header_value.lower().startswith("digest"):
        return None, {}

    params = {}
    for key, value in re.findall(r'(\w+)="([^"]*)"', header_value):
        params[key] = value
    return "digest", params


def _build_rtsp_auth_header(
    scheme: str | None,
    params: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
) -> str | None:
    if scheme == "basic":
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    if scheme != "digest":
        return None

    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop")
    opaque = params.get("opaque")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode("utf-8")).hexdigest()
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']

    if qop:
        nc = "00000001"
        cnonce = os.urandom(8).hex()
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()
        parts.extend(
            [
                f'response="{response}"',
                f'qop={qop}',
                f'nc={nc}',
                f'cnonce="{cnonce}"',
            ]
        )
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
        parts.append(f'response="{response}"')

    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def probe_rtsp_stream(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    path: str,
    timeout: float = 2.0,
) -> bool:
    uri = f"rtsp://{host}:{port}{path}"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        base_request = (
            f"DESCRIBE {uri} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: VisionHub/1.0\r\n"
            f"Accept: application/sdp\r\n\r\n"
        )
        sock.sendall(base_request.encode("utf-8"))
        response_text = _read_rtsp_response(sock)
        if " 200 " in response_text:
            return True
        if " 401 " not in response_text:
            return False

    scheme, params = _parse_auth_header(response_text)
    auth_header = _build_rtsp_auth_header(scheme, params, username, password, "DESCRIBE", uri)
    if not auth_header:
        return False

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        auth_request = (
            f"DESCRIBE {uri} RTSP/1.0\r\n"
            f"CSeq: 2\r\n"
            f"User-Agent: VisionHub/1.0\r\n"
            f"Accept: application/sdp\r\n"
            f"Authorization: {auth_header}\r\n\r\n"
        )
        sock.sendall(auth_request.encode("utf-8"))
        authenticated_response = _read_rtsp_response(sock)
        return " 200 " in authenticated_response


def discover_generic_rtsp_channels(
    *,
    host: str,
    rtsp_port: int,
    username: str,
    password: str,
    path_template: str = "/cam/realmonitor?channel={channel}&subtype=0",
    max_channels: int = 32,
    timeout: float = 1.5,
    break_after_consecutive_failures: int = 4,
) -> list[dict]:
    channels: list[dict] = []
    consecutive_failures = 0

    for channel in range(1, max_channels + 1):
        path = path_template.format(channel=channel, subtype=0)
        try:
            ok = probe_rtsp_stream(
                host=host,
                port=rtsp_port,
                username=username,
                password=password,
                path=path,
                timeout=timeout,
            )
        except Exception:
            ok = False

        if ok:
            channels.append({"channel": channel, "name": f"Canal {channel}"})
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if channels and consecutive_failures >= break_after_consecutive_failures:
                break

    return channels


def discover_dahua_channels(
    *,
    host: str,
    port: int = 80,
    username: str,
    password: str,
    use_ssl: bool = False,
    verify_ssl: bool = False,
    timeout: int = 5,
) -> list[dict]:
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"
    response = requests.get(
        url,
        auth=HTTPDigestAuth(username, password),
        timeout=timeout,
        verify=verify_ssl,
    )
    response.raise_for_status()

    channels: list[dict] = []
    for line in response.text.splitlines():
        match = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)", line.strip())
        if not match:
            continue
        channel_index = int(match.group(1))
        channel_name = match.group(2).strip() or f"Canal {channel_index + 1}"
        channels.append({"channel": channel_index + 1, "name": channel_name})

    channels.sort(key=lambda item: item["channel"])
    return channels


def discover_hikvision_channels(
    host: str,
    port: int,
    username: str,
    password: str,
    use_ssl: bool = False,
    verify_ssl: bool = False,
    timeout: int = 10,
) -> list[dict]:
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}/ISAPI/Streaming/channels"
    response = requests.get(
        url,
        auth=HTTPDigestAuth(username, password),
        timeout=timeout,
        verify=verify_ssl,
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    channels: list[dict] = []

    for node in root.iter():
        if _strip_namespace(node.tag) != "StreamingChannel":
            continue

        stream_id = None
        channel_name = None
        for child in list(node):
            name = _strip_namespace(child.tag)
            text = (child.text or "").strip()
            if name == "id":
                stream_id = text
            elif name == "channelName":
                channel_name = text

        if not stream_id or not stream_id.endswith("01"):
            continue

        channel = int(stream_id[:-2])
        channels.append(
            {
                "channel": channel,
                "name": channel_name or f"Canal {channel}",
                "stream_id": stream_id,
            }
        )

    channels.sort(key=lambda item: item["channel"])
    return channels


def user_can_view_camera(user: User, camera_id: int, db: Session) -> bool:
    if user.is_admin:
        return True
    profile_stmt = (
        select(ProfileCameraPermission)
        .join(UserProfile, UserProfile.profile_id == ProfileCameraPermission.profile_id)
        .where(
            UserProfile.user_id == user.id,
            ProfileCameraPermission.camera_id == camera_id,
            ProfileCameraPermission.can_view_live.is_(True),
        )
    )
    if db.scalar(profile_stmt) is not None:
        return True
    stmt = select(CameraPermission).where(
        CameraPermission.user_id == user.id,
        CameraPermission.camera_id == camera_id,
        CameraPermission.can_view_live.is_(True),
    )
    return db.scalar(stmt) is not None


def user_can_view_snapshot(user: User, camera_id: int, db: Session) -> bool:
    if user.is_admin:
        return True
    profile_stmt = (
        select(ProfileCameraPermission)
        .join(UserProfile, UserProfile.profile_id == ProfileCameraPermission.profile_id)
        .where(
            UserProfile.user_id == user.id,
            ProfileCameraPermission.camera_id == camera_id,
            ProfileCameraPermission.can_view_snapshot.is_(True),
        )
    )
    if db.scalar(profile_stmt) is not None:
        return True
    stmt = select(CameraPermission).where(
        CameraPermission.user_id == user.id,
        CameraPermission.camera_id == camera_id,
        CameraPermission.can_view_snapshot.is_(True),
    )
    return db.scalar(stmt) is not None


def user_can_manage_camera(user: User, camera_id: int, db: Session) -> bool:
    if user.is_admin:
        return True
    profile_stmt = (
        select(ProfileCameraPermission)
        .join(UserProfile, UserProfile.profile_id == ProfileCameraPermission.profile_id)
        .where(
            UserProfile.user_id == user.id,
            ProfileCameraPermission.camera_id == camera_id,
            ProfileCameraPermission.can_manage.is_(True),
        )
    )
    if db.scalar(profile_stmt) is not None:
        return True
    stmt = select(CameraPermission).where(
        CameraPermission.user_id == user.id,
        CameraPermission.camera_id == camera_id,
        CameraPermission.can_manage.is_(True),
    )
    return db.scalar(stmt) is not None


def list_visible_cameras(user: User, db: Session) -> Iterable[Camera]:
    if user.is_admin:
        return db.scalars(select(Camera).order_by(Camera.name)).all()

    direct_stmt = (
        select(Camera)
        .join(CameraPermission, CameraPermission.camera_id == Camera.id)
        .where(
            CameraPermission.user_id == user.id,
            CameraPermission.can_view_live.is_(True),
        )
    )
    profile_stmt = (
        select(Camera)
        .join(ProfileCameraPermission, ProfileCameraPermission.camera_id == Camera.id)
        .join(UserProfile, UserProfile.profile_id == ProfileCameraPermission.profile_id)
        .where(
            UserProfile.user_id == user.id,
            ProfileCameraPermission.can_view_live.is_(True),
        )
    )
    ids = set()
    cameras = []
    for camera in db.scalars(direct_stmt).all() + db.scalars(profile_stmt).all():
        if camera.id in ids:
            continue
        ids.add(camera.id)
        cameras.append(camera)
    cameras.sort(key=lambda item: item.name)
    return cameras


def sort_cameras_for_user(user: User, cameras: list[Camera], db: Session) -> list[Camera]:
    if not cameras:
        return cameras
    stmt = select(UserCameraLayout).where(
        UserCameraLayout.user_id == user.id,
        UserCameraLayout.camera_id.in_([camera.id for camera in cameras]),
    )
    layouts = {item.camera_id: item.position for item in db.scalars(stmt).all()}
    return sorted(cameras, key=lambda camera: (layouts.get(camera.id, 10_000), camera.name))
