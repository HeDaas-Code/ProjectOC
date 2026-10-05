"""Development-only safeguards for the SQLite fallback.

SQLite is useful for isolated local development and browser E2E runs, but it
only has one writer at a time.  The application deliberately uses PostgreSQL
in production.  When several browser clients exercise the same local Django
process, serialising write requests prevents a long-lived sync-state write from
turning a concurrent canvas PATCH into a transient ``database is locked``
error.  This is a process-local guard; it is not a replacement for PostgreSQL
or for distributed locking.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from django.db import connection
from django.http import HttpRequest, HttpResponse


_SQLITE_WRITE_LOCK = threading.RLock()
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SQLiteWriteLockMiddleware:
    """Serialise mutating requests only when the active DB is SQLite."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if connection.vendor != "sqlite" or request.method.upper() not in _WRITE_METHODS:
            return self.get_response(request)

        with _SQLITE_WRITE_LOCK:
            return self.get_response(request)
