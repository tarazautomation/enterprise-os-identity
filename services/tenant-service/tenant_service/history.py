"""The change history, and the locks that keep concurrent changes apart.

A change to Keycloak is recorded before it's made: the event is saved as "started", then marked "done" or
"failed". So a change that fails half way (roles added, then the removal fails) still has an event saying who
tried what. Invites aren't members yet; their events use the member id "invite:<invite id>".

Locks: the last-admin rule and the syncs read, then change. Two admins removing each other at the same moment
could each see the other still there and leave nobody. Changes that touch admins take the "admins" lock first;
each sync run takes its own lock and is skipped when another replica holds it. On Postgres these are advisory
locks, so they hold across replicas; a process lock covers the single-process case (and the tests' SQLite).
"""

import threading
import zlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import MemberEvent


@contextmanager
def recorded(session: Session, tenant_id: str, member_id: str, action: str, detail: dict | None = None,
             actor_id: str | None = None, actor_name: str | None = None) -> Iterator[MemberEvent]:
    """Save the event as started, run the change, then mark it done (or failed, and re-raise)."""
    ev = MemberEvent(tenant_id=tenant_id, member_id=member_id, action=action, actor_id=actor_id, actor_name=actor_name,
                     detail={**(detail or {}), "outcome": "started"})
    session.add(ev)
    session.commit()
    try:
        yield ev
    except Exception as e:
        session.rollback()
        ev.detail = {**ev.detail, "outcome": "failed", "error": type(e).__name__}
        session.commit()
        raise
    ev.detail = {**ev.detail, "outcome": "done"}
    session.commit()


class Locks:
    """Named locks: Postgres advisory locks when the database is Postgres, plus a process lock either way."""

    def __init__(self, engine):
        self._engine = engine
        self._local: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _key(self, name: str) -> int:
        return zlib.crc32(f"tenant-service:{name}".encode())

    def _process_lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._local.setdefault(name, threading.Lock())

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        """Wait for the lock, run, release."""
        with self._process_lock(name):
            if self._engine.dialect.name != "postgresql":
                yield
                return
            with self._engine.connect() as c:           # its own connection, so commits elsewhere don't release it
                c.execute(text("select pg_advisory_lock(:k)"), {"k": self._key(name)})
                try:
                    yield
                finally:
                    c.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key(name)})
                    c.commit()

    @contextmanager
    def try_hold(self, name: str) -> Iterator[bool]:
        """Take the lock if it's free. Yields whether it was taken; the caller skips its work if not."""
        lock = self._process_lock(name)
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            if self._engine.dialect.name != "postgresql":
                yield True
                return
            with self._engine.connect() as c:
                got = c.execute(text("select pg_try_advisory_lock(:k)"), {"k": self._key(name)}).scalar()
                try:
                    yield bool(got)
                finally:
                    if got:
                        c.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key(name)})
                    c.commit()
        finally:
            lock.release()
