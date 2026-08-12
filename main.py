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
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import database
from tiktok_checker import check_all_accounts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")

CHECK_INTERVAL_SECONDS = 90  # cek tiap 1.5 menit -- sesuaikan sesuai kebutuhan

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"


def load_accounts() -> list:
    """
    Baca daftar akun dari accounts.json.
    Mendukung 2 format:
    - Format baru (dengan region): [{"username": "...", "region": "jogja"}, ...]
    - Format lama (list string saja): ["user1", "user2", ...] -> otomatis
      dianggap region "jogja" supaya tetap kompatibel.
    """
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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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


@app.post("/api/check-now")
async def trigger_manual_check():
    """Untuk trigger cek manual dari dashboard (tombol refresh), di luar jadwal."""
    asyncio.create_task(run_check_cycle())
    return {"status": "started"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
