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

# Jeda antar pengecekan akun (detik), supaya tidak membombardir sekaligus.
# Dinaikkan dari 2 -> 3 detik karena dengan makin banyak akun (40+),
# permintaan yang terlalu rapat mulai memicu error/rate-limit dari TikTok.
DELAY_BETWEEN_ACCOUNTS = 3

# Kalau pengecekan sebuah akun gagal karena error tak dikenal (bukan
# "offline" atau "tidak ditemukan" yang sudah pasti), coba ulang sekali
# lagi sebelum benar-benar menyerah -- banyak kegagalan sifatnya cuma
# gangguan sesaat (timeout, koneksi terputus), bukan masalah permanen.
MAX_RETRIES = 2
RETRY_DELAY = 4


async def _check_once(username: str) -> dict:
    """Satu kali percobaan cek status live (tanpa retry)."""
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
                # fetch_room_info() dari library sudah mengembalikan objek "data"
                # langsung (bukan nested lagi), tapi kita tetap jaga-jaga.
                data = room_info if isinstance(room_info, dict) else {}
                if "data" in data and isinstance(data["data"], dict):
                    data = data["data"]

                result["title"] = data.get("title")

                # Verifikasi silang: field "status" pada data room ini
                # (bukan hasil fetch_is_live() yang dipakai di atas) kadang
                # lebih akurat/terkini. Nilai status == 4 artinya sesi live
                # sudah RESMI berakhir menurut TikTok -- kalau ternyata
                # begitu, timpa is_live jadi False meskipun fetch_is_live()
                # tadi sempat bilang True (mengurangi kasus "masih ke-detect
                # live padahal sudah selesai").
                room_status = data.get("status")
                if room_status == 4:
                    result["is_live"] = False
                    logger.info(f"[{username}] fetch_is_live=True tapi "
                                f"room status=4 (sudah berakhir) -- dikoreksi jadi offline")

                # Viewer count -- CATATAN: jumlah penonton real-time TikTok
                # kemungkinan besar TIDAK tersedia lewat endpoint statis ini;
                # itu data yang biasanya cuma dikirim lewat koneksi live
                # (WebSocket) selama live berlangsung. Field di bawah dicoba
                # sebagai kemungkinan, tapi sering akan tetap None -- ini
                # keterbatasan pendekatan "cek sekali ambil", bukan bug.
                viewer_count = (
                    data.get("user_count")
                    or (data.get("stats") or {}).get("user_count")
                    or (data.get("room") or {}).get("user_count")
                )
                result["viewer_count"] = viewer_count

                owner = data.get("owner") or data.get("user") or {}
                avatar = owner.get("avatar_thumb") or owner.get("avatar_medium") or {}
                url_list = avatar.get("url_list") or []
                result["avatar_url"] = url_list[0] if url_list else None

                if not result["avatar_url"]:
                    logger.info(f"[{username}] avatar_url tidak ditemukan. "
                                f"Key tersedia di data: {list(data.keys())}")
            except Exception as e:
                logger.warning(f"[{username}] gagal ambil detail room: "
                                f"{type(e).__name__}: {e}")

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
            # Selalu catat TIPE exception + pesannya (bukan cuma pesan) --
            # ini penting untuk diagnosis kalau errornya bukan hal yang
            # sudah dikenali di atas.
            result["error"] = f"{type(e).__name__}: {e}"
            logger.error(f"[{username}] error saat cek status: {type(e).__name__}: {e}")

    finally:
        try:
            await client.web.close()
        except Exception as e:
            # Kalau sesi belum sempat terbuka (mis. request pertama sudah gagal),
            # close() bisa error -- ini tidak fatal, cukup dicatat saja.
            logger.debug(f"[{username}] close() gagal (biasanya tidak masalah): {e}")

    return result


async def check_single_account(username: str) -> dict:
    """
    Cek status live satu akun, dengan retry otomatis kalau gagal karena
    error tak dikenal (bukan "offline" atau "tidak ditemukan" yang sudah
    pasti -- itu tidak perlu diulang karena hasilnya sudah jelas).
    """
    last_result = None
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _check_once(username)

        # Berhasil, atau errornya sudah pasti (bukan gangguan sesaat) -> selesai
        if result["error"] is None or result["error"] == "Username tidak ditemukan":
            return result

        last_result = result
        if attempt < MAX_RETRIES:
            logger.info(f"[{username}] percobaan {attempt} gagal, coba lagi...")
            await asyncio.sleep(RETRY_DELAY)

    return last_result


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
