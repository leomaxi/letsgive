import time
from collections import defaultdict

from fastapi import HTTPException, status


class RateLimiter:
    """Fixed-window request limiter (spec 8: Authentication -> "rate limiting").

    In-process only, like SessionBroadcaster -- fine for a single server,
    but a horizontally-scaled deployment needs a shared store (Redis, per
    spec 9's recommended architecture) so limits apply across processes.
    Swapping this class's internals for a Redis-backed version is the only
    change needed; callers only see check()/reset().
    """

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_seconds
        attempts = [t for t in self._attempts[key] if t > window_start]
        if len(attempts) >= self.max_attempts:
            self._attempts[key] = attempts
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Try again later.",
            )
        attempts.append(now)
        self._attempts[key] = attempts

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)

    def clear_all(self) -> None:
        self._attempts.clear()


# Keyed by email: the primary account-takeover vector this defends against
# is password/MFA-code guessing against one account, not general traffic
# shaping (that belongs at a reverse proxy / WAF layer in production).
login_rate_limiter = RateLimiter(max_attempts=10, window_seconds=15 * 60)

# Keyed by client IP: catches credential stuffing spread across many
# different email addresses from one source, which the email-keyed limiter
# above can't see (each targeted email individually stays under its own
# threshold). A higher ceiling than the email-keyed limiter since one IP can
# legitimately represent many real users behind NAT/a shared office network;
# this isn't a replacement for it, just a second, broader net. On a
# successful login only the email-keyed counter is cleared -- clearing this
# one too would let an attacker who eventually guesses one account right
# reopen the flood gate for every other email from the same IP.
login_ip_rate_limiter = RateLimiter(max_attempts=30, window_seconds=15 * 60)
