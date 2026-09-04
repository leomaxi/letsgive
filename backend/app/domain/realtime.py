import asyncio
from collections import defaultdict
from typing import Any


class SessionBroadcaster:
    """In-process pub/sub for public projection updates (spec 9: 'Realtime').

    Each connected display client gets its own asyncio.Queue; publishing
    fans a message out to every queue subscribed to that session_id. This
    is intentionally process-local -- fine for a single-process dev/pilot
    deployment, but a horizontally-scaled deployment needs a shared bus
    (Redis pub/sub, per spec 9's recommended architecture) so a publish on
    one process reaches subscribers connected to another. Swapping the
    body of subscribe/publish for a Redis-backed version is the only
    change needed; callers already only see queue-shaped semantics.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        self._subscribers[session_id].discard(queue)
        if not self._subscribers[session_id]:
            self._subscribers.pop(session_id, None)

    async def publish(self, session_id: str, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(session_id, ())):
            if queue.full():
                # A slow/stalled client shouldn't block live updates for
                # everyone else; drop its oldest pending frame instead.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await queue.put(message)


_broadcaster = SessionBroadcaster()


def get_broadcaster() -> SessionBroadcaster:
    return _broadcaster
