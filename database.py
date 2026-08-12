"""
database.py
Lapisan penyimpanan data menggunakan SQLite.
Menyimpan status terkini tiap akun + riwayat sesi live.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
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
                last_attempt TEXT,
                last_error TEXT,
                live_since TEXT
            )
        """)
        # Migrasi ringan untuk database lama yang belum punya kolom ini
        # (supaya tidak perlu hapus data lama saat kode di-update).
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(account_status)")}
        if "last_attempt" not in existing_cols:
            conn.execute("ALTER TABLE account_status ADD COLUMN last_attempt TEXT")
        if "last_error" not in existing_cols:
            conn.execute("ALTER TABLE account_status ADD COLUMN last_error TEXT")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS viewer_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                viewer_count INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_session
            ON viewer_snapshots(session_id)
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
                    (username, region, is_live, title, viewer_count, avatar_url,
                     last_checked, last_attempt, last_error, live_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """, (username, region, int(is_live), title, viewer_count, avatar_url, now, now,
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
                    last_checked = ?, last_attempt = ?, last_error = NULL,
                    live_since = CASE
                        WHEN ? = 1 THEN COALESCE(?, live_since)
                        ELSE NULL
                    END
                WHERE username = ?
            """, (region, int(is_live), title, viewer_count, avatar_url, now, now,
                  int(is_live), new_live_since, username))

        # Kelola riwayat sesi live
        session_id = None
        if is_live and not was_live:
            cursor = conn.execute("""
                INSERT INTO live_history (username, started_at, peak_viewer_count, title)
                VALUES (?, ?, ?, ?)
            """, (username, now, viewer_count, title))
            session_id = cursor.lastrowid
        elif not is_live and was_live:
            conn.execute("""
                UPDATE live_history
                SET ended_at = ?
                WHERE username = ? AND ended_at IS NULL
            """, (now, username))
        elif is_live and was_live:
            if viewer_count is not None:
                conn.execute("""
                    UPDATE live_history
                    SET peak_viewer_count = MAX(COALESCE(peak_viewer_count, 0), ?)
                    WHERE username = ? AND ended_at IS NULL
                """, (viewer_count, username))
            row = conn.execute("""
                SELECT id FROM live_history
                WHERE username = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1
            """, (username,)).fetchone()
            session_id = row["id"] if row else None

        # Catat "foto" jumlah penonton di titik waktu ini -- ini yang jadi
        # dasar grafik Viewer Trend. Cuma dicatat kalau sedang live DAN
        # angka penontonnya berhasil terbaca (viewer_count tidak None).
        if is_live and viewer_count is not None and session_id is not None:
            conn.execute("""
                INSERT INTO viewer_snapshots (session_id, username, checked_at, viewer_count)
                VALUES (?, ?, ?, ?)
            """, (session_id, username, now, viewer_count))


def touch_attempt(username: str, region: str, error_message: str):
    """
    Dipanggil saat pengecekan sebuah akun GAGAL (error/timeout/rate-limit).
    Tidak mengubah status is_live/title/viewer_count (karena kita tidak
    tahu kondisi terbaru), tapi tetap mencatat KAPAN percobaan terakhir
    dilakukan dan APA error-nya -- supaya dashboard bisa menunjukkan
    "data ini mungkin usang" alih-alih diam-diam menampilkan status lama
    seolah-olah baru.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM account_status WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            # Akun belum pernah berhasil dicek sama sekali -- buat entry
            # kosong supaya tetap muncul di dashboard (offline by default)
            # dan errornya terlihat, bukan hilang sama sekali dari daftar.
            conn.execute("""
                INSERT INTO account_status
                    (username, region, is_live, last_attempt, last_error)
                VALUES (?, ?, 0, ?, ?)
            """, (username, region, now, error_message))
        else:
            conn.execute("""
                UPDATE account_status
                SET last_attempt = ?, last_error = ?
                WHERE username = ?
            """, (now, error_message, username))


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


def get_leaderboard(days: int = 7, region: str = None):
    """
    Ranking akun berdasarkan aktivitas live dalam N hari terakhir:
    - session_count: berapa kali mulai live
    - avg_viewers / max_viewers: dari peak_viewer_count tiap sesi
    - last_live_at: kapan terakhir kali live

    Cuma menghitung sesi yang MULAI dalam rentang waktu tsb (started_at),
    termasuk sesi yang masih berlangsung (ended_at masih NULL).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    query = """
        SELECT
            lh.username,
            a.region AS region,
            a.avatar_url AS avatar_url,
            COUNT(*) AS session_count,
            AVG(lh.peak_viewer_count) AS avg_viewers,
            MAX(lh.peak_viewer_count) AS max_viewers,
            MAX(lh.started_at) AS last_live_at
        FROM live_history lh
        LEFT JOIN account_status a ON a.username = lh.username
        WHERE lh.started_at >= ?
    """
    params = [cutoff]

    if region and region != "all":
        query += " AND a.region = ?"
        params.append(region)

    query += " GROUP BY lh.username ORDER BY session_count DESC, avg_viewers DESC"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # Bulatkan rata-rata penonton supaya rapi ditampilkan (mis. 42.3 -> 42)
            d["avg_viewers"] = round(d["avg_viewers"]) if d["avg_viewers"] is not None else None
            results.append(d)
        return results


WIB_OFFSET_HOURS = 7  # WIB = UTC+7 (tanpa DST, jadi aman dihitung statis)


def get_heatmap(days: int = 30, region: str = None):
    """
    Menghitung berapa kali tiap kombinasi (hari-dalam-minggu, jam) muncul
    sesi live dimulai, dalam N hari terakhir -- dasar untuk heatmap
    "jam & hari favorit live" kompetitor.

    Waktu dikonversi ke WIB (UTC+7) supaya relevan untuk tim di Yogyakarta,
    bukan ditampilkan dalam UTC yang membingungkan.

    Return: list of {"day": 0-6 (Senin=0..Minggu=6), "hour": 0-23, "count": N}
    Semua 168 kombinasi (7x24) selalu ada di hasil, termasuk yang count=0,
    supaya frontend tidak perlu mengisi kekosongan sendiri.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    query = """
        SELECT lh.started_at AS started_at
        FROM live_history lh
        LEFT JOIN account_status a ON a.username = lh.username
        WHERE lh.started_at >= ?
    """
    params = [cutoff]

    if region and region != "all":
        query += " AND a.region = ?"
        params.append(region)

    # Inisialisasi semua 168 sel ke 0 dulu
    counts = {(day, hour): 0 for day in range(7) for hour in range(24)}

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        for r in rows:
            try:
                dt_utc = datetime.fromisoformat(r["started_at"])
                dt_wib = dt_utc + timedelta(hours=WIB_OFFSET_HOURS)
                day = dt_wib.weekday()  # Senin=0 ... Minggu=6
                hour = dt_wib.hour
                counts[(day, hour)] += 1
            except (ValueError, TypeError):
                continue  # lewati baris dengan format waktu yang rusak

    return [
        {"day": day, "hour": hour, "count": count}
        for (day, hour), count in sorted(counts.items())
    ]


def get_viewer_trend(username: str, limit: int = 200):
    """
    Data grafik Viewer Trend untuk SATU akun: rangkaian jumlah penonton
    dari waktu ke waktu, untuk sesi live TERBARU (baik yang masih
    berlangsung maupun yang sudah selesai).
    """
    with get_conn() as conn:
        session = conn.execute("""
            SELECT id, started_at, ended_at, peak_viewer_count
            FROM live_history
            WHERE username = ?
            ORDER BY started_at DESC LIMIT 1
        """, (username,)).fetchone()

        if session is None:
            return {"session": None, "snapshots": []}

        rows = conn.execute("""
            SELECT checked_at, viewer_count FROM viewer_snapshots
            WHERE session_id = ?
            ORDER BY checked_at ASC LIMIT ?
        """, (session["id"], limit)).fetchall()

        return {
            "session": dict(session),
            "snapshots": [dict(r) for r in rows],
        }


def get_insights(days: int = 7):
    """
    Hasilkan beberapa "insight" otomatis dari data yang sudah ada --
    BUKAN lewat panggilan AI/LLM eksternal, murni aturan logika dari
    angka-angka yang sudah tersimpan. Ini membuatnya cepat, gratis, dan
    hasilnya selalu bisa dijelaskan/ditelusuri baliknya.
    """
    insights = {}
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()

    with get_conn() as conn:
        # 1. Performa tertinggi saat ini -- akun live dengan viewer_count tertinggi
        top_live = conn.execute("""
            SELECT username, viewer_count FROM account_status
            WHERE is_live = 1 AND viewer_count IS NOT NULL
            ORDER BY viewer_count DESC LIMIT 1
        """).fetchone()
        insights["top_performer"] = dict(top_live) if top_live else None

        # 2. Viewer spike -- akun dengan pertumbuhan % tercepat dalam sesi
        #    live yang SEDANG berlangsung (bandingkan snapshot pertama vs
        #    terakhir dalam 20 menit terakhir).
        spike_cutoff = (now - timedelta(minutes=20)).isoformat()
        open_sessions = conn.execute("""
            SELECT id, username FROM live_history WHERE ended_at IS NULL
        """).fetchall()

        best_spike = None
        for s in open_sessions:
            snaps = conn.execute("""
                SELECT viewer_count, checked_at FROM viewer_snapshots
                WHERE session_id = ? AND checked_at >= ?
                ORDER BY checked_at ASC
            """, (s["id"], spike_cutoff)).fetchall()
            if len(snaps) < 2:
                continue
            first, last = snaps[0]["viewer_count"], snaps[-1]["viewer_count"]
            if first <= 0:
                continue
            growth_pct = round(((last - first) / first) * 100)
            if growth_pct > 0 and (best_spike is None or growth_pct > best_spike["growth_pct"]):
                best_spike = {
                    "username": s["username"],
                    "from": first,
                    "to": last,
                    "growth_pct": growth_pct,
                }
        insights["viewer_spike"] = best_spike

        # 3. Waktu optimal -- jam dengan total sesi live terbanyak (7 hari terakhir)
        heatmap = get_heatmap(days=7, region="all")
        best_hour_entry = max(heatmap, key=lambda h: h["count"], default=None)
        if best_hour_entry and best_hour_entry["count"] > 0:
            h = best_hour_entry["hour"]
            insights["optimal_time"] = {
                "hour_range": f"{h:02d}:00 - {(h+1)%24:02d}:00 WIB",
                "session_count": best_hour_entry["count"],
            }
        else:
            insights["optimal_time"] = None

        # 4. Topik populer -- kata yang paling sering muncul di judul live
        #    (di luar kata umum/stopword), plus rata-rata viewer sesi yang
        #    mengandung kata itu dibanding rata-rata keseluruhan.
        titles_rows = conn.execute("""
            SELECT title, peak_viewer_count FROM live_history
            WHERE started_at >= ? AND title IS NOT NULL AND title != ''
        """, (cutoff,)).fetchall()

        stopwords = {
            "yang", "dan", "di", "ke", "dari", "untuk", "live", "ini", "itu",
            "dengan", "saja", "ada", "juga", "akan", "kita", "kami", "the",
            "a", "an", "to", "of", "in", "on", "for", "and", "is", "are"
        }
        word_stats = {}  # word -> {"count": n, "viewer_sum": n, "viewer_n": n}
        for row in titles_rows:
            words = [w.strip(".,!?()[]#@").lower() for w in row["title"].split()]
            seen_in_title = set()
            for w in words:
                if len(w) < 3 or w in stopwords or w in seen_in_title:
                    continue
                seen_in_title.add(w)
                stats = word_stats.setdefault(w, {"count": 0, "viewer_sum": 0, "viewer_n": 0})
                stats["count"] += 1
                if row["peak_viewer_count"] is not None:
                    stats["viewer_sum"] += row["peak_viewer_count"]
                    stats["viewer_n"] += 1

        if word_stats:
            top_word, top_stats = max(word_stats.items(), key=lambda kv: kv[1]["count"])
            overall_avg = (
                sum(r["peak_viewer_count"] for r in titles_rows if r["peak_viewer_count"] is not None)
                / max(1, len([r for r in titles_rows if r["peak_viewer_count"] is not None]))
            )
            word_avg = top_stats["viewer_sum"] / top_stats["viewer_n"] if top_stats["viewer_n"] else None
            diff_pct = round(((word_avg - overall_avg) / overall_avg) * 100) if (word_avg and overall_avg) else None
            insights["popular_topic"] = {
                "word": top_word,
                "mention_count": top_stats["count"],
                "avg_viewers_for_topic": round(word_avg) if word_avg else None,
                "diff_pct_vs_overall": diff_pct,
            }
        else:
            insights["popular_topic"] = None

    return insights
