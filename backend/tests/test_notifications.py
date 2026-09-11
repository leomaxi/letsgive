from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.domain.notifications import MXROUTE_SMTP_HOST, LoggingNotifier, SmtpNotifier, _build_default_notifier


async def test_smtp_notifier_sends_a_real_message_with_correct_envelope():
    smtp_instance = MagicMock()
    with patch("app.domain.notifications.smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__enter__.return_value = smtp_instance
        notifier = SmtpNotifier(
            host="smtp.example.org",
            port=587,
            username="letsgive",
            password="hunter2",
            from_email="noreply@letsgive.ca",
            use_tls=True,
        )
        await notifier.send_otp(to_email="finance@example.org", code="123456", session_id="sess-1")

    smtp_cls.assert_called_once_with("smtp.example.org", 587, timeout=10)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("letsgive", "hunter2")
    sent_message = smtp_instance.send_message.call_args[0][0]
    assert sent_message["To"] == "finance@example.org"
    assert sent_message["From"] == "noreply@letsgive.ca"
    assert "123456" in sent_message.get_payload()
    assert "sess-1" in sent_message.get_payload()


async def test_smtp_notifier_skips_login_and_starttls_when_not_configured():
    smtp_instance = MagicMock()
    with patch("app.domain.notifications.smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__enter__.return_value = smtp_instance
        notifier = SmtpNotifier(
            host="localhost",
            port=1025,
            username="",
            password="",
            from_email="noreply@letsgive.ca",
            use_tls=False,
        )
        await notifier.send_otp(to_email="finance@example.org", code="999999", session_id="sess-2")

    smtp_instance.starttls.assert_not_called()
    smtp_instance.login.assert_not_called()
    smtp_instance.send_message.assert_called_once()


def test_default_notifier_stays_logging_only_when_smtp_host_unset():
    fake_settings = SimpleNamespace(smtp_provider="", smtp_host="")
    with patch("app.domain.notifications.get_settings", return_value=fake_settings):
        notifier = _build_default_notifier()
    assert isinstance(notifier, LoggingNotifier)


def test_default_notifier_switches_to_smtp_once_host_is_configured():
    fake_settings = SimpleNamespace(
        smtp_provider="",
        smtp_host="smtp.example.org",
        smtp_port=2525,
        smtp_username="u",
        smtp_password="p",
        smtp_from_email="noreply@letsgive.ca",
        smtp_use_tls=False,
    )
    with patch("app.domain.notifications.get_settings", return_value=fake_settings):
        notifier = _build_default_notifier()
    assert isinstance(notifier, SmtpNotifier)


async def test_mxroute_provider_uses_mxroute_host_and_username_as_default_sender():
    smtp_instance = MagicMock()
    fake_settings = SimpleNamespace(
        smtp_provider="mxroute",
        smtp_host="",
        smtp_port=587,
        smtp_username="otp@example.org",
        smtp_password="mailbox-password",
        smtp_from_email="noreply@letsgive.ca",
        smtp_use_tls=True,
    )
    with (
        patch("app.domain.notifications.get_settings", return_value=fake_settings),
        patch("app.domain.notifications.smtplib.SMTP") as smtp_cls,
    ):
        smtp_cls.return_value.__enter__.return_value = smtp_instance
        notifier = _build_default_notifier()
        await notifier.send_otp(to_email="finance@example.org", code="123456", session_id="sess-mx")

    smtp_cls.assert_called_once_with(MXROUTE_SMTP_HOST, 587, timeout=10)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("otp@example.org", "mailbox-password")
    sent_message = smtp_instance.send_message.call_args[0][0]
    assert sent_message["From"] == "otp@example.org"
    assert sent_message["To"] == "finance@example.org"
