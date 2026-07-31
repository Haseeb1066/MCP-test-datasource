"""Per-request Tableau viewer identity (Connected App JWT sub)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_tableau_username: ContextVar[str | None] = ContextVar("tableau_username", default=None)


def get_tableau_username() -> str | None:
    v = _tableau_username.get()
    if v is None:
        return None
    cleaned = v.strip()
    return cleaned or None


def set_tableau_username(username: str | None) -> None:
    _tableau_username.set((username or "").strip() or None)


@contextmanager
def tableau_user_context(username: str | None) -> Iterator[None]:
    token = _tableau_username.set((username or "").strip() or None)
    try:
        yield
    finally:
        _tableau_username.reset(token)
