"""
main.py
Entry point backend: FastAPI app + scheduler background yang mengecek
status live 20 akun kompetitor secara berkala.

Jalankan lokal:
    uvicorn main:app --reload --port 8000

Endpoint:
    GET /api/status            -> status terkini semua akun
    GET /api/history           -> riwayat sesi live (semua akun)
    GET /api/history/{username}-> riwayat sesi live akun tertentu
    POST /api/check-now        -> trigger pengecekan manual (di luar jadwal)
"""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

import database
from tiktok_checker import check_all_accounts
import export

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")

CHECK_INTERVAL_SECONDS = 360  # dinaikkan lagi ke 360 detik (6 menit) --
                               # 403 Forbidden masih berlanjut meski sudah
                               # diperlambat ke 180 detik. Ini langkah lebih
                               # agresif untuk memulihkan "reputasi" IP server.
                               # Bisa diturunkan lagi nanti kalau blokir reda.

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"


def load_accounts_from_file() -> list:
    """
    Baca daftar akun dari accounts.json -- HANYA dipakai untuk seed awal
    (migrasi 1x ke database), bukan lagi sumber utama sehari-hari.
    Mendukung 2 format lama untuk kompatibilitas seed.
    """
    if not ACCOUNTS_FILE.exists():
        return []
    with open(ACCOUNTS_FILE, "r") as f:
        raw = json.load(f)["accounts"]

    accounts = []
    for item in raw:
        if isinstance(item, str):
            accounts.append({"username": item, "region": "jogja"})
        else:
            accounts.append({
                "username": item["username"],
                "region": item.get("region", "jogja"),
            })
    return accounts


def load_accounts() -> list:
    """
    Daftar akun yang dipantau -- SUMBER UTAMA sekarang adalah database
    (tabel monitored_accounts), bukan lagi accounts.json. Ini supaya
    tambah/hapus akun lewat dashboard tersimpan PERMANEN (di Volume),
    tidak hilang saat redeploy seperti kalau ditulis ke file biasa.
    """
    return database.get_monitored_accounts()


async def run_check_cycle():
    """Cek semua akun lalu simpan hasilnya ke database."""
    accounts = load_accounts()
    logger.info(f"Mulai pengecekan {len(accounts)} akun...")

    usernames = [a["username"] for a in accounts]
    region_map = {a["username"]: a["region"] for a in accounts}

    results = await check_all_accounts(usernames)

    for r in results:
        if r["error"]:
            logger.warning(f"  {r['username']}: ERROR - {r['error']}")
            # Tetap catat "percobaan terakhir" meski gagal, supaya
            # dashboard tahu data ini mungkin usang (bukan diam-diam
            # menampilkan status lama seolah baru saja dicek).
            database.touch_attempt(
                username=r["username"],
                region=region_map.get(r["username"], "jogja"),
                error_message=r["error"],
            )
            continue
        database.upsert_status(
            username=r["username"],
            is_live=r["is_live"],
            region=region_map.get(r["username"], "jogja"),
            title=r["title"],
            viewer_count=r["viewer_count"],
            avatar_url=r["avatar_url"],
        )
        if r["is_live"]:
            logger.info(f"  {r['username']}: LIVE ({r['viewer_count']} viewers)")

    logger.info("Pengecekan selesai.")


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()

    # Migrasi 1x: kalau tabel monitored_accounts masih kosong (fitur ini
    # baru pertama kali aktif), isi dari accounts.json lama supaya daftar
    # akun yang sudah dikelola selama ini tidak hilang.
    seed = load_accounts_from_file()
    if seed:
        was_seeded = database.seed_monitored_accounts_if_empty(seed)
        if was_seeded:
            logger.info(f"Migrasi: {len(seed)} akun dipindahkan dari accounts.json ke database.")

    scheduler.add_job(run_check_cycle, "interval", seconds=CHECK_INTERVAL_SECONDS,
                       id="check_cycle")
    scheduler.start()
    # Jalankan satu kali langsung saat startup (jangan tunggu interval pertama)
    asyncio.create_task(run_check_cycle())
    logger.info(f"Scheduler aktif, interval {CHECK_INTERVAL_SECONDS} detik.")
    yield
    scheduler.shutdown()


app = FastAPI(title="TikTok Live Competitor Monitor", lifespan=lifespan)

# CORS -- longgarkan origin frontend sesuai domain Vercel kamu nanti
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ganti dengan domain Vercel spesifik saat production
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


class NewAccountRequest(BaseModel):
    username: str
    region: str = "jogja"

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip().lstrip("@")
        # Username TikTok: huruf, angka, titik, underscore, 2-24 karakter
        if not re.match(r"^[a-zA-Z0-9._]{2,24}$", v):
            raise ValueError(
                "Username tidak valid. Gunakan huruf, angka, titik, atau "
                "underscore saja (2-24 karakter), tanpa spasi atau @."
            )
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        if v not in ("jogja", "luar_jogja"):
            raise ValueError("Region harus 'jogja' atau 'luar_jogja'")
        return v


@app.get("/api/accounts")
def list_accounts():
    """Daftar akun yang sedang dipantau (dari database, bukan file lagi)."""
    return {"accounts": database.get_monitored_accounts()}


@app.post("/api/accounts")
def create_account(payload: NewAccountRequest):
    """Tambah akun baru ke daftar pantauan."""
    added = database.add_monitored_account(payload.username, payload.region)
    if not added:
        raise HTTPException(status_code=409, detail=f"@{payload.username} sudah ada di daftar pantauan")
    logger.info(f"Akun baru ditambahkan lewat dashboard: {payload.username} ({payload.region})")
    return {"status": "added", "username": payload.username, "region": payload.region}


@app.delete("/api/accounts/{username}")
def delete_account(username: str):
    """Hapus akun dari daftar pantauan (riwayat live tetap tersimpan)."""
    removed = database.remove_monitored_account(username)
    if not removed:
        raise HTTPException(status_code=404, detail=f"@{username} tidak ditemukan di daftar pantauan")
    logger.info(f"Akun dihapus lewat dashboard: {username}")
    return {"status": "removed", "username": username}


@app.get("/api/status")
def get_status():
    return {"accounts": database.get_all_status()}


@app.get("/api/history")
def get_history_all():
    return {"history": database.get_history()}


@app.get("/api/history/{username}")
def get_history_for_user(username: str, limit: int = 50):
    accounts = load_accounts()
    usernames = [a["username"] for a in accounts]
    if username not in usernames:
        raise HTTPException(status_code=404, detail="Akun tidak ada di daftar pantauan")
    return {"history": database.get_history(username=username, limit=limit)}


@app.get("/api/leaderboard")
def get_leaderboard(days: int = 7, region: str = "all"):
    """
    Ranking akun berdasarkan aktivitas live dalam N hari terakhir.
    Query params: ?days=7&region=jogja (region opsional, default 'all')
    """
    if days not in (7, 30):
        raise HTTPException(status_code=400, detail="Parameter 'days' harus 7 atau 30")
    return {"leaderboard": database.get_leaderboard(days=days, region=region)}


@app.get("/api/heatmap")
def get_heatmap(days: int = 30, region: str = "all"):
    """
    Data heatmap jam & hari favorit live kompetitor, dalam N hari terakhir.
    Query params: ?days=30&region=jogja (region opsional, default 'all')
    """
    if days not in (7, 30):
        raise HTTPException(status_code=400, detail="Parameter 'days' harus 7 atau 30")
    return {"heatmap": database.get_heatmap(days=days, region=region)}


@app.get("/api/viewer-trend/{username}")
def get_viewer_trend(username: str):
    """
    Grafik viewer trend untuk sesi live terbaru sebuah akun.
    """
    accounts = load_accounts()
    usernames = [a["username"] for a in accounts]
    if username not in usernames:
        raise HTTPException(status_code=404, detail="Akun tidak ada di daftar pantauan")
    return database.get_viewer_trend(username)


@app.get("/api/insights")
def get_insights(days: int = 7):
    """
    Insight otomatis berbasis aturan logika (bukan panggilan AI eksternal).
    """
    return {"insights": database.get_insights(days=days)}


@app.get("/api/export/excel")
def export_excel(days: int = 7, region: str = "all"):
    """
    Generate laporan Excel (.xlsx) berisi ringkasan, status terkini,
    leaderboard, dan riwayat live. File dibuat di memori (tidak disimpan
    ke disk), langsung dikirim sebagai download.
    """
    if days not in (7, 30):
        raise HTTPException(status_code=400, detail="Parameter 'days' harus 7 atau 30")

    buffer = export.build_report(days=days, region=region)
    filename = f"laporan-live-monitor-{days}hari.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/check-now")
async def trigger_manual_check():
    """Untuk trigger cek manual dari dashboard (tombol refresh), di luar jadwal."""
    asyncio.create_task(run_check_cycle())
    return {"status": "started"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
