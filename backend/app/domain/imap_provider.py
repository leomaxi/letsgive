import asyncio
import imaplib
import re

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


def _get_baseline_uid_sync(*, host: str, port: int, mailbox: str, password: str) -> int:
    try:
        connection = imaplib.IMAP4_SSL(host, port, timeout=10)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ImapAuthError(f"Could not reach {host}:{port} ({exc}).") from exc

    try:
        typ, _ = connection.login(mailbox, password)
        if typ != "OK":
            raise ImapAuthError("The mail server rejected that email and password.")
        typ, data = connection.status("INBOX", "(UIDNEXT)")
        if typ != "OK" or not data or data[0] is None:
            return 0
        match = re.search(rb"UIDNEXT (\d+)", data[0])
        uidnext = int(match.group(1)) if match else 1
        return max(uidnext - 1, 0)
    except imaplib.IMAP4.error as exc:
        raise ImapAuthError(
            "Login failed. If this is Gmail, Outlook, Yahoo, or iCloud, make sure you're using "
            "an app-specific password (not your regular account password) and that IMAP access "
            "is turned on for the account."
        ) from exc
    except OSError as exc:
        # A network hiccup (timeout, connection reset, DNS blip) *after* the
        # socket was already open -- the outer try/except above only guards
        # the initial connect. Without this, a mid-call network error would
        # propagate as a raw, unhandled exception all the way out of the API
        # endpoint (a 500 with no useful message) instead of the same clean,
        # user-facing error a login failure already gets.
        raise ImapAuthError(f"Lost the connection to {host}:{port} ({exc}).") from exc
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, login already succeeded/failed
            pass


async def connect_and_get_baseline_uid(*, host: str, port: int, mailbox: str, password: str) -> int:
    """Validates the login (raises ImapAuthError on failure) and returns the
    UID watermark a brand-new connection should start from: everything
    already sitting in the mailbox at connect time is treated as "not new",
    so connecting an inbox that's been in use for years doesn't dump its
    entire history into the ledger. Contrast with a connection that
    predates this watermark existing at all (NULL), which does a one-time
    full sweep instead -- see MailboxConnection.imap_last_uid and
    poll_imap_connection. Run off the event loop the same way SmtpNotifier
    runs smtplib -- one blocking stdlib call, not a hot path.
    """
    return await asyncio.to_thread(
        _get_baseline_uid_sync, host=host, port=port, mailbox=mailbox, password=password
    )
