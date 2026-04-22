from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, SessionLocal, engine, get_db
from app.dependencies import get_current_user, require_admin
from app.go2rtc import (
    GO2RTC_BASE_URL,
    GO2RTC_CONFIG_PATH,
    build_public_go2rtc_base_url,
    camera_stream_name,
    delete_go2rtc_stream,
    go2rtc_api_available,
    sync_go2rtc_api,
    sync_go2rtc_config,
    upsert_go2rtc_stream,
)
from app.models import Camera, CameraPermission, Profile, ProfileCameraPermission, User, UserCameraLayout, UserProfile
from app.notifications import send_user_welcome_email
from app.seed import seed_admin
from app.security import hash_password, verify_password
from app.services import (
    build_rtsp_url,
    discover_dahua_channels,
    discover_generic_rtsp_channels,
    discover_hikvision_channels,
    fetch_camera_snapshot,
    fetch_isapi_info,
    list_visible_cameras,
    sort_cameras_for_user,
    user_can_manage_camera,
    user_can_view_camera,
    user_can_view_snapshot,
)


BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="Gestao de Cameras Hikvision")
app.add_middleware(SessionMiddleware, secret_key="troque-esta-chave-em-producao")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/assets", StaticFiles(directory=BASE_DIR / "assets"), name="assets")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_camera_columns()
    ensure_user_columns()
    db = SessionLocal()
    try:
        seed_admin(db)
        sync_go2rtc_config(db)
        sync_go2rtc_api(db)
    finally:
        db.close()


def ensure_camera_columns() -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("cameras")}
    alter_statements = []
    if "device_name" not in columns:
        alter_statements.append("ALTER TABLE cameras ADD COLUMN device_name VARCHAR(120) NOT NULL DEFAULT ''")
    if "rtsp_url" not in columns:
        alter_statements.append("ALTER TABLE cameras ADD COLUMN rtsp_url VARCHAR(500) NOT NULL DEFAULT ''")
    if "snapshot_url" not in columns:
        alter_statements.append("ALTER TABLE cameras ADD COLUMN snapshot_url VARCHAR(500) NOT NULL DEFAULT ''")
    if "use_ssl" not in columns:
        alter_statements.append("ALTER TABLE cameras ADD COLUMN use_ssl BOOLEAN NOT NULL DEFAULT 0")
    if not alter_statements:
        return
    with engine.begin() as connection:
        for statement in alter_statements:
            connection.execute(text(statement))
        if "device_name" not in columns:
            connection.execute(text("UPDATE cameras SET device_name = name WHERE device_name = ''"))


def ensure_user_columns() -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("users")}
    alter_statements = []
    if "email" not in columns:
        alter_statements.append("ALTER TABLE users ADD COLUMN email VARCHAR(255) NOT NULL DEFAULT ''")
    if not alter_statements:
        return
    with engine.begin() as connection:
        for statement in alter_statements:
            connection.execute(text(statement))


def build_users_context(db: Session, current_user: User, error: str | None = None, success: str | None = None) -> dict:
    users = db.scalars(select(User).order_by(User.username)).all()
    cameras = db.scalars(select(Camera).order_by(Camera.name)).all()
    profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
    user_profiles = db.scalars(
        select(UserProfile).options(joinedload(UserProfile.profile), joinedload(UserProfile.user))
    ).all()
    profile_permissions = db.scalars(
        select(ProfileCameraPermission).options(
            joinedload(ProfileCameraPermission.profile),
            joinedload(ProfileCameraPermission.camera),
        )
    ).all()
    permissions = db.scalars(
        select(CameraPermission).options(
            joinedload(CameraPermission.user),
            joinedload(CameraPermission.camera),
        )
    ).all()
    return {
        "current_user": current_user,
        "users": users,
        "cameras": cameras,
        "profiles": profiles,
        "user_profiles": user_profiles,
        "profile_permissions": profile_permissions,
        "permissions": permissions,
        "error": error,
        "success": success,
    }


def render(request: Request, template_name: str, **context):
    context.setdefault("current_path", request.url.path)
    return templates.TemplateResponse(template_name, {"request": request, **context})


def build_device_view_url(
    *,
    device_type: str,
    host: str,
    port: int,
    username: str,
    use_ssl: bool,
    message: str | None = None,
    error: str | None = None,
) -> str:
    params = {
        "device_type": device_type,
        "host": host,
        "port": port,
        "username": username,
        "use_ssl": "true" if use_ssl else "false",
    }
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    return f"/devices/view?{urlencode(params)}"


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username))
    if not user or not verify_password(password, user.password_hash):
        return render(request, "login.html", error="Usuario ou senha invalidos.")

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cameras = list_visible_cameras(current_user, db)
    cameras = sort_cameras_for_user(current_user, list(cameras), db)
    snapshot_camera_ids = {
        permission.camera_id
        for permission in current_user.camera_permissions
        if permission.can_view_snapshot
    }
    if current_user.is_admin:
        snapshot_camera_ids = {
            camera.id for camera in cameras if camera.snapshot_url or camera.isapi_enabled
        }
    else:
        snapshot_camera_ids = {
            camera.id
            for camera in cameras
            if camera.id in snapshot_camera_ids and (camera.snapshot_url or camera.isapi_enabled)
        }
    auto_stream = len(cameras) <= 10
    return render(
        request,
        "dashboard.html",
        current_user=current_user,
        cameras=cameras,
        snapshot_camera_ids=snapshot_camera_ids,
        go2rtc_base_url=build_public_go2rtc_base_url(request.url.scheme, request.headers.get("host", "")),
        camera_stream_name=camera_stream_name,
        build_rtsp_url=build_rtsp_url,
        auto_stream=auto_stream,
    )


@app.get("/cameras", response_class=HTMLResponse)
def cameras_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.is_admin:
        cameras = db.scalars(select(Camera).order_by(Camera.name)).all()
    else:
        cameras = list_visible_cameras(current_user, db)
    return render(request, "cameras.html", current_user=current_user, cameras=cameras)


@app.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.is_admin:
        cameras = db.scalars(
            select(Camera)
            .where(Camera.device_type.in_(["generic_rtsp", "hikvision_dvr", "hikvision_nvr", "unv"]))
            .order_by(Camera.name)
        ).all()
    else:
        cameras = [
            camera
            for camera in list_visible_cameras(current_user, db)
            if camera.device_type in {"generic_rtsp", "hikvision_dvr", "hikvision_nvr", "unv"}
        ]

    device_groups = defaultdict(list)
    for camera in cameras:
        device_key = (
            camera.device_type,
            camera.host,
            camera.port,
            camera.username,
            camera.use_ssl,
        )
        device_groups[device_key].append(camera)

    grouped_devices = []
    for key, items in device_groups.items():
        device_type, host, port, username, use_ssl = key
        device_names = [item.device_name.strip() for item in items if item.device_name.strip()]
        if device_names:
            device_name = max(set(device_names), key=device_names.count)
        else:
            device_name = f"{device_type} {host}"
        grouped_devices.append(
            {
                "device_name": device_name,
                "device_type": device_type,
                "host": host,
                "port": port,
                "username": username,
                "use_ssl": use_ssl,
                "cameras": sorted(items, key=lambda item: item.channel),
            }
        )
    grouped_devices.sort(key=lambda item: (item["host"], item["device_type"]))

    return render(
        request,
        "devices.html",
        current_user=current_user,
        grouped_devices=grouped_devices,
    )


@app.get("/devices/view", response_class=HTMLResponse)
def device_view(
    request: Request,
    device_type: str,
    host: str,
    port: int,
    username: str = "",
    use_ssl: bool = False,
    message: str | None = None,
    error: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Camera).where(
        Camera.device_type == device_type,
        Camera.host == host,
        Camera.port == port,
        Camera.username == username,
        Camera.use_ssl == use_ssl,
    ).order_by(Camera.channel, Camera.name)
    cameras = db.scalars(stmt).all()
    if not current_user.is_admin:
        cameras = [camera for camera in cameras if user_can_view_camera(current_user, camera.id, db)]
    device_name = cameras[0].device_name if cameras else host
    return render(
        request,
        "device_detail.html",
        current_user=current_user,
        cameras=cameras,
        device_name=device_name,
        device_type=device_type,
        host=host,
        port=port,
        username=username,
        use_ssl=use_ssl,
        message=message,
        error=error,
    )


@app.post("/cameras")
def create_camera(
    name: str = Form(""),
    description: str = Form(""),
    device_type: str = Form(...),
    rtsp_url: str = Form(""),
    host: str = Form(""),
    port: int = Form(80),
    rtsp_port: int = Form(554),
    use_ssl: bool = Form(False),
    username: str = Form(""),
    password: str = Form(""),
    channel: int = Form(1),
    subtype: int = Form(0),
    isapi_enabled: bool = Form(False),
    rtsp_path_template: str = Form("/cam/realmonitor?channel={channel}&subtype={subtype}"),
    snapshot_path: str = Form("/ISAPI/Streaming/channels/{stream_id}/picture"),
    snapshot_url: str = Form(""),
    verify_ssl: bool = Form(False),
    auto_discover: bool = Form(False),
    device_name: str = Form(""),
    return_to: str = Form("cameras"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rtsp_url = rtsp_url.strip()
    if device_type in {"generic_rtsp", "unv"} and rtsp_url:
        parsed = urlsplit(rtsp_url)
        if parsed.hostname and not host:
            host = parsed.hostname
        if parsed.port:
            rtsp_port = parsed.port
        if parsed.username and not username:
            username = parsed.username
        if parsed.password and not password:
            password = parsed.password
    if device_type in {"generic_rtsp", "unv"}:
        isapi_enabled = False
        if not rtsp_url:
            if not host or not username or not password:
                raise HTTPException(
                    status_code=400,
                    detail="Para RTSP generico ou UNV, informe IP, porta RTSP, usuario e senha.",
                )
            device_label = device_name.strip() or name.strip() or (f"UNV {host}" if device_type == "unv" else f"RTSP {host}")
            port = 80 if not port else port
            rtsp_port = rtsp_port or port or 554
            if auto_discover:
                if device_type == "unv":
                    discovered_channels = discover_generic_rtsp_channels(
                        host=host,
                        rtsp_port=rtsp_port,
                        username=username,
                        password=password,
                        path_template="/unicast/c{channel}/s0/live",
                    )
                else:
                    try:
                        discovered_channels = discover_dahua_channels(
                            host=host,
                            port=port,
                            username=username,
                            password=password,
                            use_ssl=False,
                            verify_ssl=False,
                        )
                    except Exception:
                        discovered_channels = discover_generic_rtsp_channels(
                            host=host,
                            rtsp_port=rtsp_port,
                            username=username,
                            password=password,
                            path_template="/cam/realmonitor?channel={channel}&subtype=0",
                        )

                if discovered_channels:
                    rtsp_template = (
                        "/unicast/c{channel}/s{subtype}/live"
                        if device_type == "unv"
                        else "/cam/realmonitor?channel={channel}&subtype={subtype}"
                    )
                    for item in discovered_channels:
                        channel_number = item["channel"]
                        camera_name = f"{device_label} - Canal {channel_number}"
                        existing = db.scalar(
                            select(Camera).where(
                                Camera.host == host,
                                Camera.channel == channel_number,
                                Camera.device_type == device_type,
                                Camera.username == username,
                            )
                        )
                        if existing:
                            camera = existing
                        else:
                            camera = Camera(
                                name=camera_name,
                                device_name=device_label,
                                description=description or f"Descoberta automatica no dispositivo {device_label}.",
                                device_type=device_type,
                                host=host,
                                port=port,
                                rtsp_port=rtsp_port,
                                use_ssl=False,
                                username=username,
                                password=password,
                                channel=channel_number,
                                subtype=0,
                                isapi_enabled=False,
                                rtsp_path_template=rtsp_template,
                                snapshot_url="",
                                verify_ssl=False,
                            )
                            db.add(camera)
                            continue

                        camera.name = camera_name
                        camera.device_name = device_label
                        camera.description = description or f"Descoberta automatica no dispositivo {device_label}."
                        camera.host = host
                        camera.port = port
                        camera.rtsp_port = rtsp_port
                        camera.username = username
                        camera.password = password
                        camera.channel = channel_number
                        camera.subtype = 0
                        camera.isapi_enabled = False
                        camera.rtsp_url = ""
                        camera.rtsp_path_template = rtsp_template
                        camera.snapshot_url = ""
                        camera.verify_ssl = False

                    db.commit()
                    sync_go2rtc_config(db)
                    sync_go2rtc_api(db)
                    return RedirectResponse(
                        "/devices" if return_to == "devices" else "/cameras",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            if not name.strip():
                name = f"{device_label} - Canal {channel}"
    if device_type in {"hikvision_dvr", "hikvision_nvr"}:
        if not host or not username:
            raise HTTPException(status_code=400, detail="Informe host e usuario para o dispositivo Hikvision.")

        try:
            discovered_channels = discover_hikvision_channels(
                host=host,
                port=port,
                username=username,
                password=password,
                use_ssl=use_ssl,
                verify_ssl=False,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Falha ao conectar no dispositivo Hikvision em {host}:{port}. Verifique host, porta HTTP, usuario, senha e SSL. Erro: {exc}",
            ) from exc
        if not discovered_channels:
            raise HTTPException(status_code=400, detail="Nenhum canal Hikvision encontrado no dispositivo.")

        device_label = name.strip() or f"Hikvision {host}"
        for item in discovered_channels:
            camera_name = item["name"]
            if camera_name.lower() == f"canal {item['channel']}":
                camera_name = f"{device_label} - Canal {item['channel']}"

            existing = db.scalar(
                select(Camera).where(
                    Camera.host == host,
                    Camera.channel == item["channel"],
                    Camera.device_type == device_type,
                )
            )
            if existing:
                camera = existing
            else:
                camera = Camera(
                    name=camera_name,
                    device_name=device_label,
                    description=description or f"Descoberta automatica no dispositivo {device_label}.",
                    device_type=device_type,
                    host=host,
                    port=port,
                    rtsp_port=rtsp_port,
                    use_ssl=use_ssl,
                    username=username,
                    password=password,
                    channel=item["channel"],
                    subtype=0,
                    isapi_enabled=True,
                    snapshot_path="/ISAPI/Streaming/channels/{stream_id}/picture",
                    verify_ssl=False,
                )
                db.add(camera)
                continue

            camera.name = camera_name
            camera.device_name = device_label
            camera.description = description or f"Descoberta automatica no dispositivo {device_label}."
            camera.device_type = device_type
            camera.rtsp_url = ""
            camera.host = host
            camera.port = port
            camera.rtsp_port = rtsp_port
            camera.use_ssl = use_ssl
            camera.username = username
            camera.password = password
            camera.channel = item["channel"]
            camera.subtype = 0
            camera.isapi_enabled = True
            camera.snapshot_path = "/ISAPI/Streaming/channels/{stream_id}/picture"
            camera.snapshot_url = ""
            camera.verify_ssl = False

        db.commit()
        sync_go2rtc_config(db)
        sync_go2rtc_api(db)
        return RedirectResponse(
            "/devices" if return_to == "devices" else "/cameras",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not host and not rtsp_url:
        raise HTTPException(status_code=400, detail="Informe o host ou a URL RTSP completa.")
    if device_type not in {"generic_rtsp", "unv"} and not name.strip():
        raise HTTPException(status_code=400, detail="Informe um nome para a camera RTSP.")
    camera = Camera(
        name=name.strip(),
        device_name=device_name.strip() or name.strip(),
        description=description,
        device_type=device_type,
        rtsp_url=rtsp_url,
        host=host,
        port=port,
        rtsp_port=rtsp_port,
        use_ssl=use_ssl,
        username=username,
        password=password,
        channel=channel,
        subtype=subtype,
        isapi_enabled=isapi_enabled,
        rtsp_path_template=(
            "/unicast/c{channel}/s{subtype}/live"
            if device_type == "unv"
            else rtsp_path_template
        ),
        snapshot_path=snapshot_path,
        snapshot_url=snapshot_url.strip(),
        verify_ssl=verify_ssl,
    )
    db.add(camera)
    db.commit()
    sync_go2rtc_config(db)
    upsert_go2rtc_stream(camera)
    return RedirectResponse(
        "/devices" if return_to == "devices" else "/cameras",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/devices/cameras")
def create_device_camera(
    device_type: str = Form(...),
    device_name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    rtsp_port: int = Form(554),
    username: str = Form(""),
    password: str = Form(""),
    use_ssl: bool = Form(False),
    name: str = Form(...),
    description: str = Form(""),
    channel: int = Form(...),
    subtype: int = Form(0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    existing = db.scalar(
        select(Camera).where(
            Camera.device_type == device_type,
            Camera.host == host,
            Camera.port == port,
            Camera.username == username,
            Camera.use_ssl == use_ssl,
            Camera.channel == channel,
            Camera.subtype == subtype,
        )
    )
    if existing:
        return RedirectResponse(
            build_device_view_url(
                device_type=device_type,
                host=host,
                port=port,
                username=username,
                use_ssl=use_ssl,
                error=f"Ja existe uma camera no canal {channel} subtipo {subtype}.",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    camera = Camera(
        name=name.strip(),
        device_name=device_name.strip(),
        description=description.strip(),
        device_type=device_type,
        host=host,
        port=port,
        rtsp_port=rtsp_port,
        use_ssl=use_ssl,
        username=username,
        password=password,
        channel=channel,
        subtype=subtype,
        isapi_enabled=device_type in {"hikvision_dvr", "hikvision_nvr"},
        rtsp_path_template=(
            "/unicast/c{channel}/s{subtype}/live"
            if device_type == "unv"
            else "/cam/realmonitor?channel={channel}&subtype={subtype}"
        ),
        snapshot_path="/ISAPI/Streaming/channels/{stream_id}/picture",
        verify_ssl=False,
    )
    db.add(camera)
    db.commit()
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse(
        build_device_view_url(
            device_type=device_type,
            host=host,
            port=port,
            username=username,
            use_ssl=use_ssl,
            message="Camera adicionada ao dispositivo.",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/devices/cameras/{camera_id}/update")
def update_device_camera(
    camera_id: int,
    name: str = Form(...),
    description: str = Form(""),
    channel: int = Form(...),
    subtype: int = Form(0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    camera = db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera nao encontrada.")

    duplicate = db.scalar(
        select(Camera).where(
            Camera.id != camera_id,
            Camera.device_type == camera.device_type,
            Camera.host == camera.host,
            Camera.port == camera.port,
            Camera.username == camera.username,
            Camera.use_ssl == camera.use_ssl,
            Camera.channel == channel,
            Camera.subtype == subtype,
        )
    )
    if duplicate:
        return RedirectResponse(
            build_device_view_url(
                device_type=camera.device_type,
                host=camera.host,
                port=camera.port,
                username=camera.username,
                use_ssl=camera.use_ssl,
                error=f"O canal {channel} subtipo {subtype} ja esta em uso neste dispositivo.",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    delete_go2rtc_stream(camera)
    camera.name = name.strip()
    camera.description = description.strip()
    camera.channel = channel
    camera.subtype = subtype
    db.commit()
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse(
        build_device_view_url(
            device_type=camera.device_type,
            host=camera.host,
            port=camera.port,
            username=camera.username,
            use_ssl=camera.use_ssl,
            message="Camera atualizada.",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_detail(
    camera_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    camera = db.get(Camera, camera_id)
    if not camera or not user_can_view_camera(current_user, camera_id, db):
        raise HTTPException(status_code=404, detail="Camera nao encontrada.")

    isapi_info = fetch_isapi_info(camera) if camera.isapi_enabled else {}
    return render(
        request,
        "camera_detail.html",
        current_user=current_user,
        camera=camera,
        rtsp_url=build_rtsp_url(camera),
        go2rtc_base_url=build_public_go2rtc_base_url(request.url.scheme, request.headers.get("host", "")),
        go2rtc_stream_name=camera_stream_name(camera),
        can_manage=user_can_manage_camera(current_user, camera_id, db),
        can_view_snapshot=user_can_view_snapshot(current_user, camera_id, db),
        has_snapshot=bool(camera.snapshot_url or camera.isapi_enabled),
        isapi_info=isapi_info,
    )


@app.get("/cameras/{camera_id}/snapshot")
def camera_snapshot(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    camera = db.get(Camera, camera_id)
    if not camera or not user_can_view_snapshot(current_user, camera_id, db):
        raise HTTPException(status_code=404, detail="Snapshot indisponivel.")
    if not camera.snapshot_url and not camera.isapi_enabled:
        raise HTTPException(status_code=404, detail="Camera sem snapshot configurado.")

    try:
        snapshot = fetch_camera_snapshot(camera)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao buscar snapshot: {exc}") from exc

    return Response(content=snapshot.content, media_type=snapshot.headers.get("Content-Type", "image/jpeg"))


@app.post("/dashboard/layout")
async def save_dashboard_layout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payload = await request.json()
    camera_ids = payload.get("camera_ids", [])
    if not isinstance(camera_ids, list):
        raise HTTPException(status_code=400, detail="Payload invalido.")

    visible_ids = {camera.id for camera in list_visible_cameras(current_user, db)}
    submitted_ids = [int(camera_id) for camera_id in camera_ids if int(camera_id) in visible_ids]

    existing = db.scalars(select(UserCameraLayout).where(UserCameraLayout.user_id == current_user.id)).all()
    existing_map = {item.camera_id: item for item in existing}

    for position, camera_id in enumerate(submitted_ids):
        layout = existing_map.get(camera_id)
        if not layout:
            layout = UserCameraLayout(user_id=current_user.id, camera_id=camera_id, position=position)
            db.add(layout)
        else:
            layout.position = position

    db.commit()
    return {"ok": True}


@app.post("/cameras/{camera_id}/delete")
def delete_camera(
    camera_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    camera = db.get(Camera, camera_id)
    if camera:
        if camera.device_type == "generic_rtsp":
            redirect_url = "/cameras"
        else:
            redirect_url = build_device_view_url(
                device_type=camera.device_type,
                host=camera.host,
                port=camera.port,
                username=camera.username,
                use_ssl=camera.use_ssl,
                message="Camera removida do dispositivo.",
            )
        delete_go2rtc_stream(camera)
        db.delete(camera)
        db.commit()
        sync_go2rtc_config(db)
        sync_go2rtc_api(db)
        return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/cameras", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/delete")
def delete_device(
    device_type: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(""),
    use_ssl: bool = Form(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    cameras = db.scalars(
        select(Camera).where(
            Camera.device_type == device_type,
            Camera.host == host,
            Camera.port == port,
            Camera.username == username,
            Camera.use_ssl == use_ssl,
        )
    ).all()

    for camera in cameras:
        delete_go2rtc_stream(camera)
        db.delete(camera)

    db.commit()
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse("/devices", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/update")
def update_device(
    original_device_type: str = Form(...),
    original_host: str = Form(...),
    original_port: int = Form(...),
    original_username: str = Form(""),
    original_use_ssl: bool = Form(False),
    device_type: str = Form(...),
    device_name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    rtsp_port: int = Form(554),
    username: str = Form(""),
    password: str = Form(""),
    use_ssl: bool = Form(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    cameras = db.scalars(
        select(Camera).where(
            Camera.device_type == original_device_type,
            Camera.host == original_host,
            Camera.port == original_port,
            Camera.username == original_username,
            Camera.use_ssl == original_use_ssl,
        )
    ).all()

    for camera in cameras:
        delete_go2rtc_stream(camera)
        camera.device_name = device_name.strip()
        camera.device_type = device_type
        camera.host = host
        camera.port = port
        camera.rtsp_port = rtsp_port
        camera.username = username
        if password:
            camera.password = password
        camera.use_ssl = use_ssl

    db.commit()
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse(
        build_device_view_url(
            device_type=device_type,
            host=host,
            port=port,
            username=username,
            use_ssl=use_ssl,
            message="Dispositivo atualizado.",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/cameras/{camera_id}/rename")
def rename_camera(
    camera_id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    camera = db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera nao encontrada.")
    camera.name = name.strip()
    db.commit()
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse("/cameras", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return render(request, "users.html", **build_users_context(db, current_user))


@app.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    existing = db.scalar(select(User).where(User.username == username))
    if existing:
        return render(request, "users.html", **build_users_context(db, current_user, error="Usuario ja existe."))
    existing_email = db.scalar(select(User).where(User.email == email))
    if existing_email:
        return render(request, "users.html", **build_users_context(db, current_user, error="E-mail ja cadastrado."))

    user = User(
        username=username,
        email=email,
        full_name=full_name,
        password_hash=hash_password(password),
        is_admin=is_admin,
        is_active=is_active,
    )
    db.add(user)
    db.flush()
    login_url = str(request.base_url).rstrip("/") + "/login"
    try:
        send_user_welcome_email(
            recipient_email=email,
            recipient_name=full_name,
            username=username,
            password=password,
            login_url=login_url,
        )
    except Exception as exc:
        db.rollback()
        return render(
            request,
            "users.html",
            **build_users_context(
                db,
                current_user,
                error=f"Usuario nao criado. Falha no envio do e-mail SMTP: {exc}",
            ),
        )

    db.commit()
    return render(
        request,
        "users.html",
        **build_users_context(db, current_user, success=f"Usuario {username} criado e e-mail enviado para {email}."),
    )


@app.post("/users/{user_id}/permissions")
def upsert_permission(
    user_id: int,
    camera_id: int = Form(...),
    can_view_live: bool = Form(False),
    can_view_snapshot: bool = Form(False),
    can_manage: bool = Form(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(CameraPermission).where(
        CameraPermission.user_id == user_id, CameraPermission.camera_id == camera_id
    )
    permission = db.scalar(stmt)
    if not permission:
        permission = CameraPermission(user_id=user_id, camera_id=camera_id)
        db.add(permission)
    permission.can_view_live = can_view_live
    permission.can_view_snapshot = can_view_snapshot
    permission.can_manage = can_manage
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/profiles")
def create_profile(
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    existing = db.scalar(select(Profile).where(Profile.name == name))
    if existing:
        raise HTTPException(status_code=400, detail="Perfil ja existe.")
    db.add(Profile(name=name, description=description))
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/profiles/{profile_id}/permissions")
def upsert_profile_permission(
    profile_id: int,
    camera_id: int = Form(...),
    can_view_live: bool = Form(False),
    can_view_snapshot: bool = Form(False),
    can_manage: bool = Form(False),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(ProfileCameraPermission).where(
        ProfileCameraPermission.profile_id == profile_id,
        ProfileCameraPermission.camera_id == camera_id,
    )
    permission = db.scalar(stmt)
    if not permission:
        permission = ProfileCameraPermission(profile_id=profile_id, camera_id=camera_id)
        db.add(permission)
    permission.can_view_live = can_view_live
    permission.can_view_snapshot = can_view_snapshot
    permission.can_manage = can_manage
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/profiles")
def assign_profile_to_user(
    user_id: int,
    profile_id: int = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(UserProfile).where(UserProfile.user_id == user_id, UserProfile.profile_id == profile_id)
    existing = db.scalar(stmt)
    if not existing:
        db.add(UserProfile(user_id=user_id, profile_id=profile_id))
        db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/toggle")
def toggle_user_active(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user:
        user.is_active = not user.is_active
        db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/go2rtc", response_class=HTMLResponse)
def go2rtc_page(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cameras = db.scalars(select(Camera).order_by(Camera.name)).all()
    sync_go2rtc_config(db)
    streams = [
        {
            "camera": camera,
            "stream_name": camera_stream_name(camera),
            "rtsp_url": build_rtsp_url(camera),
        }
        for camera in cameras
    ]
    config_text = GO2RTC_CONFIG_PATH.read_text(encoding="utf-8") if GO2RTC_CONFIG_PATH.exists() else ""
    return render(
        request,
        "go2rtc.html",
        current_user=current_user,
        go2rtc_base_url=build_public_go2rtc_base_url(request.url.scheme, request.headers.get("host", "")),
        go2rtc_api_online=go2rtc_api_available(),
        config_path=str(GO2RTC_CONFIG_PATH.resolve()),
        streams=streams,
        config_text=config_text,
    )


@app.post("/go2rtc/sync")
def go2rtc_sync(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    sync_go2rtc_config(db)
    sync_go2rtc_api(db)
    return RedirectResponse("/go2rtc", status_code=status.HTTP_303_SEE_OTHER)
