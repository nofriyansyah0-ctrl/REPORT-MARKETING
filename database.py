"""
database.py
Lapisan penyimpanan data menggunakan SQLite.
Menyimpan status terkini tiap akun + riwayat sesi live.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Lokasi file database bisa diatur lewat environment variable DB_PATH.
# Ini penting untuk Railway: kalau ada Volume yang di-mount (misal ke
# /data), set DB_PATH=/data/monitor.db supaya data TIDAK hilang setiap
# kali redeploy. Kalau env var tidak diset (misal saat jalan di
# komputer lokal), otomatis pakai file biasa di folder backend seperti
# sebelumnya -- tidak ada yang berubah untuk pemakaian lokal.
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "monitor.db")))


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Buat tabel jika belum ada. Panggil sekali saat startup."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_status (
                username TEXT PRIMARY KEY,
                region TEXT NOT NULL DEFAULT 'jogja',
                is_live INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                viewer_count INTEGER,
                avatar_url TEXT,
                last_checked TEXT,
                live_since TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                peak_viewer_count INTEGER,
                title TEXT
            )
        """)


def upsert_status(username: str, is_live: bool, region: str = "jogja", title: str = None,
                   viewer_count: int = None, avatar_url: str = None):
    """
    Update status terkini sebuah akun.
    Kalau status berubah dari offline -> live, buka entry baru di live_history.
    Kalau berubah dari live -> offline, tutup entry yang masih terbuka.
    """
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_live FROM account_status WHERE username = ?", (username,)
        ).fetchone()

        was_live = bool(row["is_live"]) if row else False

        if row is None:
            conn.execute("""
                INSERT INTO account_status
                    (username, region, is_live, title, viewer_count, avatar_url, last_checked, live_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (username, region, int(is_live), title, viewer_count, avatar_url, now,
                  now if is_live else None))
        else:
            if is_live and not was_live:
                new_live_since = now          # baru mulai live
            elif is_live and was_live:
                new_live_since = None         # dibiarkan tak berubah (lihat COALESCE di bawah)
            else:
                new_live_since = None         # sedang tidak live

            conn.execute("""
                UPDATE account_status
                SET region = ?, is_live = ?, title = ?, viewer_count = ?, avatar_url = ?,
                    last_checked = ?,
                    live_since = CASE
                        WHEN ? = 1 THEN COALESCE(?, live_since)
                        ELSE NULL
                    END
                WHERE username = ?
            """, (region, int(is_live), title, viewer_count, avatar_url, now,
                  int(is_live), new_live_since, username))

        # Kelola riwayat sesi live
        if is_live and not was_live:
            conn.execute("""
                INSERT INTO live_history (username, started_at, peak_viewer_count, title)
                VALUES (?, ?, ?, ?)
            """, (username, now, viewer_count, title))
        elif not is_live and was_live:
            conn.execute("""
                UPDATE live_history
                SET ended_at = ?
                WHERE username = ? AND ended_at IS NULL
            """, (now, username))
        elif is_live and was_live and viewer_count is not None:
            conn.execute("""
                UPDATE live_history
                SET peak_viewer_count = MAX(COALESCE(peak_viewer_count, 0), ?)
                WHERE username = ? AND ended_at IS NULL
            """, (viewer_count, username))


def get_all_status():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM account_status ORDER BY is_live DESC, username ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(username: str = None, limit: int = 50):
    with get_conn() as conn:
        if username:
            rows = conn.execute("""
                SELECT * FROM live_history WHERE username = ?
                ORDER BY started_at DESC LIMIT ?
            """, (username, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM live_history ORDER BY started_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]
