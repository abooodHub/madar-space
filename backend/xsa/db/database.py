"""SQLite database setup + session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from xsa.db.models import Base


def _set_pragmas(dbapi_con, _):
    dbapi_con.execute("PRAGMA journal_mode=WAL")
    dbapi_con.execute("PRAGMA synchronous=NORMAL")
    dbapi_con.execute("PRAGMA foreign_keys=ON")
    dbapi_con.execute("PRAGMA temp_store=MEMORY")
    dbapi_con.execute("PRAGMA mmap_size=268435456")  # 256 MB
    dbapi_con.execute("PRAGMA cache_size=-32000")  # 32 MB


_engine = None
_SessionLocal = None


def init_db(db_path: Path) -> None:
    global _engine, _SessionLocal
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    event.listen(_engine, "connect", _set_pragmas)
    Base.metadata.create_all(_engine)
    from xsa.db.schema_migrations import apply_migrations

    try:
        with _engine.begin() as conn:
            # SQLite DDL must share an actual write transaction, including the
            # migration ledger, so a failed upgrade cannot be marked complete.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            apply_migrations(conn)
    except Exception:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
        raise
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def get_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _SessionLocal()
