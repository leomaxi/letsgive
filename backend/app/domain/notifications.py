import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger("letsgive.notifications")
# uvicorn's default logging config doesn't attach a handler to arbitrary app
# loggers, so INFO messages would silently vanish in local dev without this.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Notifier(Protocol):
    async def send_otp(self, *, to_email: str, code: str, session_id: str) -> None: ...


class LoggingNotifier:
    """Dev-only stand-in for a real email/SMS provider.

    Logs instead of delivering, so the plaintext code is never returned by any
    API response. Phase 5 (SaaS operations / notifications) replaces this with
    real provider-backed channels behind the same Notifier protocol.
    """

    async def send_otp(self, *, to_email: str, code: str, session_id: str) -> None:
        logger.info("OTP for session=%s sent to finance officer %s: %s", session_id, to_email, code)


class SmtpNotifier:
    """Real email delivery via SMTP, selected automatically once
    `LETSGIVE_SMTP_HOST` is set (see Settings.smtp_host) -- closes the "no
    real email/SMS provider" gap for the email half; SMS would need a
    separate paid provider (e.g. Twilio) and live credentials this app
    doesn't have, so it's out of scope here.

    Uses the stdlib `smtplib` synchronously, off the event loop via
    `asyncio.to_thread`, rather than pulling in an async SMTP dependency for
    what's a low-volume, latency-insensitive send (one OTP per approval
    request, not a hot path).
    """

    def __init__(
        self, *, host: str, port: int, username: str, password: str, from_email: str, use_tls: bool
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_email = from_email
        self._use_tls = use_tls

    async def send_otp(self, *, to_email: str, code: str, session_id: str) -> None:
        await asyncio.to_thread(self._send_sync, to_email=to_email, code=code, session_id=session_id)

    def _send_sync(self, *, to_email: str, code: str, session_id: str) -> None:
        message = MIMEText(
            f"A live-session approval code was requested for session {session_id}.\n\n"
            f"Code: {code}\n\n"
            "If you weren't expecting this, you can ignore it -- the code expires on its "
            "own and nothing is authorized until it's entered."
        )
        message["Subject"] = "Let's Give: finance approval code"
        message["From"] = self._from_email
        message["To"] = to_email

        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


def _build_default_notifier() -> Notifier:
    settings = get_settings()
    if settings.smtp_host:
        return SmtpNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_email=settings.smtp_from_email,
            use_tls=settings.smtp_use_tls,
        )
    return LoggingNotifier()


_notifier: Notifier = _build_default_notifier()


def get_notifier() -> Notifier:
    return _notifier


def set_notifier(notifier: Notifier) -> None:
    """Test/deployment hook to swap the process-wide notifier implementation."""
    global _notifier
    _notifier = notifier
