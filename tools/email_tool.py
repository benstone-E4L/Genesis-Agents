"""Email tool - send email via SendGrid or SMTP."""
from __future__ import annotations
import logging
import os
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)

_DEFAULT_FROM = "agents@swarmsync.ai"


async def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    sender = from_email or _DEFAULT_FROM

    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if sendgrid_key:
        try:
            import httpx

            payload = {
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": sender},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {sendgrid_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
            return {"ok": True, "provider": "sendgrid", "to": to, "subject": subject}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__, "message": str(e)}

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if smtp_host and smtp_user and smtp_pass:
        try:
            import asyncio
            import smtplib
            from email.mime.text import MIMEText

            smtp_port = int(os.environ.get("SMTP_PORT", "587"))

            def _send_smtp() -> None:
                msg = MIMEText(body, "plain")
                msg["Subject"] = subject
                msg["From"] = sender
                msg["To"] = to

                if smtp_port == 465:
                    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                        server.login(smtp_user, smtp_pass)
                        server.sendmail(sender, [to], msg.as_string())
                else:
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.ehlo()
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.sendmail(sender, [to], msg.as_string())

            await asyncio.to_thread(_send_smtp)
            return {"ok": True, "provider": "smtp", "to": to, "subject": subject}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__, "message": str(e)}

    return {
        "ok": False,
        "error": "missing_env: SENDGRID_API_KEY",
        "hint": "set SENDGRID_API_KEY to enable this tool (or set SMTP_HOST + SMTP_USER + SMTP_PASS)",
    }


SEND_EMAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_email",
        "description": (
            "Send an email. Uses SendGrid if SENDGRID_API_KEY is set, "
            "otherwise falls back to SMTP (SMTP_HOST + SMTP_USER + SMTP_PASS)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Plain-text email body.",
                },
                "from_email": {
                    "type": "string",
                    "description": "Sender email address (defaults to agents@swarmsync.ai).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
}


def register() -> None:
    register_tool("send_email", send_email, SEND_EMAIL_SCHEMA)
