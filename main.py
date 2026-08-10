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
    with open(ACCOUNTS_FILE, "r") as f:
        return json.load(f)["accounts"]


async def run_check_cycle():
    """Cek semua akun lalu simpan hasilnya ke database."""
    usernames = load_accounts()
    logger.info(f"Mulai pengecekan {len(usernames)} akun...")

    results = await check_all_accounts(usernames)

    for r in results:
        if r["error"]:
            logger.warning(f"  {r['username']}: ERROR - {r['error']}")
            continue
        database.upsert_status(
            username=r["username"],
            is_live=r["is_live"],
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
                       id="check_cycle", next_run_time=None)
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
    allow_origins=["https://dashboard-chi-sooty-60.vercel.app"],  # ganti dengan domain Vercel spesifik saat production
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
def get_history_for_user(username: str):
    accounts = load_accounts()
    if username not in accounts:
        raise HTTPException(status_code=404, detail="Akun tidak ada di daftar pantauan")
    return {"history": database.get_history(username=username)}


@app.post("/api/check-now")
async def trigger_manual_check():
    """Untuk trigger cek manual dari dashboard (tombol refresh), di luar jadwal."""
    asyncio.create_task(run_check_cycle())
    return {"status": "started"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
