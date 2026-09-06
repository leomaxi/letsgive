import asyncio
import imaplib

# Best-effort IMAP host/port for well-known providers, so connecting a
# Gmail/Outlook/Yahoo/iCloud address never requires typing a hostname --
# only a custom-domain mailbox needs one supplied manually.
_COMMON_IMAP_HOSTS: dict[str, tuple[str, int]] = {
    "gmail.com": ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "outlook.com": ("outlook.office365.com", 993),
    "hotmail.com": ("outlook.office365.com", 993),
    "live.com": ("outlook.office365.com", 993),
    "msn.com": ("outlook.office365.com", 993),
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "yahoo.co.uk": ("imap.mail.yahoo.com", 993),
    "ymail.com": ("imap.mail.yahoo.com", 993),
    "icloud.com": ("imap.mail.me.com", 993),
    "me.com": ("imap.mail.me.com", 993),
}


def guess_imap_host(mailbox: str) -> tuple[str, int] | None:
    domain = mailbox.rsplit("@", 1)[-1].lower() if "@" in mailbox else ""
    return _COMMON_IMAP_HOSTS.get(domain)


class ImapAuthError(Exception):
    """A live IMAP login attempt failed. The message is written to be shown
    to the user directly (no credentials or stack trace in it).
    """


def _login_sync(*, host: str, port: int, mailbox: str, password: str) -> None:
    try:
        connection = imaplib.IMAP4_SSL(host, port, timeout=10)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ImapAuthError(f"Could not reach {host}:{port} ({exc}).") from exc

    try:
        typ, _ = connection.login(mailbox, password)
        if typ != "OK":
            raise ImapAuthError("The mail server rejected that email and password.")
    except imaplib.IMAP4.error as exc:
        raise ImapAuthError(
            "Login failed. If this is Gmail, Outlook, Yahoo, or iCloud, make sure you're using "
            "an app-specific password (not your regular account password) and that IMAP access "
            "is turned on for the account."
        ) from exc
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, login already succeeded/failed
            pass


async def verify_imap_login(*, host: str, port: int, mailbox: str, password: str) -> None:
    """Raises ImapAuthError on failure; returns normally on a successful
    login+logout. Run off the event loop the same way SmtpNotifier runs
    smtplib -- one blocking stdlib call, not a hot path.
    """
    await asyncio.to_thread(_login_sync, host=host, port=port, mailbox=mailbox, password=password)
