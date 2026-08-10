"""
tiktok_checker.py
Mengecek status live tiap akun TikTok pakai library TikTokLive.

CATATAN PENTING:
- Ini memakai endpoint TIDAK RESMI milik TikTok (bukan API resmi/publik).
  Endpoint bisa berubah sewaktu-waktu tanpa pemberitahuan, dan struktur
  response (nama field di dalam room_info) bisa berbeda-beda -- kode di
  bawah mengakses field dengan aman (pakai .get()) untuk mengantisipasi itu.
- Karena ini scraping, pakai jeda antar-request (sudah diatur di bawah)
  supaya tidak membebani/memicu rate-limit dari sisi TikTok.
- Untuk pemakaian internal 20 akun, risiko biasanya kecil, tapi tetap
  pantau log kalau mulai banyak error/timeout.
"""

import asyncio
import logging

from TikTokLive import TikTokLiveClient

logger = logging.getLogger("tiktok_checker")

# Jeda antar pengecekan akun (detik), supaya tidak membombardir sekaligus
DELAY_BETWEEN_ACCOUNTS = 2


async def check_single_account(username: str) -> dict:
    """
    Cek status live satu akun.
    Return dict: {username, is_live, title, viewer_count, avatar_url, error}
    """
    result = {
        "username": username,
        "is_live": False,
        "title": None,
        "viewer_count": None,
        "avatar_url": None,
        "error": None,
    }

    client = TikTokLiveClient(unique_id=f"@{username}")

    try:
        is_live = await client.web.fetch_is_live(unique_id=username)
        result["is_live"] = is_live

        if is_live:
            try:
                room_info = await client.web.fetch_room_info(unique_id=username)
                # Struktur response TikTok bisa nested; ambil dengan aman.
                data = room_info.get("data", room_info) if isinstance(room_info, dict) else {}
                result["title"] = data.get("title")
                stats = data.get("stats") or {}
                result["viewer_count"] = stats.get("user_count")
                owner = data.get("owner") or {}
                avatar = owner.get("avatar_thumb") or {}
                url_list = avatar.get("url_list") or []
                result["avatar_url"] = url_list[0] if url_list else None
            except Exception as e:
                logger.warning(f"[{username}] gagal ambil detail room: {e}")

    except Exception as e:
        # Library ini melempar exception generik untuk user offline/tidak
        # ditemukan/error jaringan -- dibedakan lewat isi pesannya.
        msg = str(e).lower()
        if "offline" in msg:
            result["is_live"] = False
        elif "not found" in msg or "does not exist" in msg:
            result["error"] = "Username tidak ditemukan"
            logger.warning(f"[{username}] tidak ditemukan di TikTok")
        else:
            result["error"] = str(e)
            logger.error(f"[{username}] error saat cek status: {e}")

    finally:
        await client.web.close()

    return result


async def check_all_accounts(usernames: list) -> list:
    """
    Cek status semua akun secara berurutan (bukan paralel) dengan jeda,
    untuk menghindari rate-limit dari TikTok.
    """
    results = []
    for username in usernames:
        result = await check_single_account(username)
        results.append(result)
        await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS)
    return results
