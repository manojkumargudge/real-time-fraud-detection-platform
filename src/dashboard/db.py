"""Database connections for the dashboard: every read or write borrows its own connection.

Streamlit runs each browser session on its own thread, so a single shared connection would be
used concurrently. ``Database.connection()`` hands out a connection per call and closes it after
use, so no connection is ever shared between sessions or threads.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg


class Database:
    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        self.dsn = dsn
        self.connect_timeout = connect_timeout

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """A connection for one read or write (autocommit; writes open their own transaction)."""
        conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=self.connect_timeout)
        try:
            yield conn
        finally:
            conn.close()
