import os
import threading

from django.apps import AppConfig
from django.db.backends.signals import connection_created


_SQLITE_WAL_LOCK = threading.Lock()
_SQLITE_WAL_DATABASES: set[str] = set()


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self):
        connection_created.connect(_configure_sqlite_connection, dispatch_uid="projectoc.sqlite-tuning")


def _configure_sqlite_connection(sender, connection, **kwargs):
    """Tune local SQLite without racing WAL initialisation between threads.

    Production uses PostgreSQL.  ``journal_mode=WAL`` changes persistent
    database state and can itself take the SQLite writer lock, so it is only
    issued once per database path in this Django process.  Every connection
    still receives a busy timeout.
    """
    if connection.vendor != "sqlite":
        return
    timeout_ms = max(1000, int(float(os.getenv("SQLITE_BUSY_TIMEOUT", "20000"))))
    database_name = str(connection.settings_dict.get("NAME") or ":memory:")
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
        if database_name == ":memory:":
            return
    with _SQLITE_WAL_LOCK:
        if database_name in _SQLITE_WAL_DATABASES:
            return
        with connection.cursor() as cursor:
            # WAL lets readers continue while short local sync writes commit.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        _SQLITE_WAL_DATABASES.add(database_name)
