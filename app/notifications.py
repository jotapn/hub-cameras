from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def send_user_welcome_email(
    *,
    recipient_email: str,
    recipient_name: str,
    username: str,
    password: str,
    login_url: str,
) -> None:
    host = os.getenv("EMAIL_HOST", "").strip()
    port = int(os.getenv("EMAIL_PORT", "465"))
    smtp_user = os.getenv("EMAIL_HOST_USER", "").strip()
    smtp_password = os.getenv("EMAIL_HOST_PASSWORD", "")
    default_from = os.getenv("DEFAULT_FROM_EMAIL", smtp_user).strip()
    use_ssl = os.getenv("EMAIL_USE_SSL", "true").lower() == "true"
    use_tls = os.getenv("EMAIL_USE_TLS", "false").lower() == "true"

    if not host or not default_from:
        raise RuntimeError("SMTP nao configurado corretamente no arquivo .env.")

    message = EmailMessage()
    message["Subject"] = "Acesso ao Vision Hub"
    message["From"] = default_from
    message["To"] = recipient_email
    message.set_content(
        "\n".join(
            [
                f"Ola, {recipient_name}.",
                "",
                "Seu acesso ao Vision Hub foi criado com sucesso.",
                f"URL: {login_url}",
                f"Usuario: {username}",
                f"Senha: {password}",
                "",
                "Por seguranca, altere a senha apos o primeiro acesso.",
            ]
        )
    )

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=15) as server:
        if use_tls:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_password)
        server.send_message(message)
