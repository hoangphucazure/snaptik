import os
import re
import asyncio
import aiohttp
from fastapi import FastAPI, Request
from telegram import (
    Bot,
    Update,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)

TIKTOK_URL_PATTERN = re.compile(
    r"(https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+)"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


async def resolve_short_url(session: aiohttp.ClientSession, url: str) -> str:
    """Mở rộng link rút gọn (vt.tiktok.com / vm.tiktok.com) thành link gốc."""
    if "vt.tiktok.com" in url or "vm.tiktok.com" in url:
        try:
            async with session.get(
                url, headers=HEADERS, allow_redirects=True, timeout=10
            ) as resp:
                return str(resp.url).split("?")[0]
        except Exception:
            return url
    return url


async def fetch_tiktok_data(tiktok_url: str) -> tuple[dict | None, str]:
    """Gọi API TikWM lấy dữ liệu video hoặc slideshow ảnh không watermark."""
    api_url = "https://www.tikwm.com/api/"

    async with aiohttp.ClientSession() as session:
        clean_url = await resolve_short_url(session, tiktok_url)
        payload = {"url": clean_url, "hd": 1}

        for _ in range(3):
            try:
                async with session.post(
                    api_url, data=payload, headers=HEADERS, timeout=15
                ) as response:
                    if response.status != 200:
                        await asyncio.sleep(1.2)
                        continue
                    result = await response.json()
                    if result.get("code") == 0 and result.get("data"):
                        return result["data"], ""
                    if "limit" in str(result.get("msg", "")).lower():
                        await asyncio.sleep(1.2)
                        continue
                    return None, result.get("msg", "Không lấy được dữ liệu.")
            except Exception:
                await asyncio.sleep(1.2)

    return None, "Máy chủ đang bận, vui lòng thử lại sau vài giây."


@app.get("/{full_path:path}")
async def health_check(full_path: str = ""):
    return {"status": "TikTok Video & Photo Downloader Bot is running!"}


@app.post("/{full_path:path}")
async def telegram_webhook(request: Request, full_path: str = ""):
    data = await request.json()
    update = Update.de_json(data, bot)

    # 1. Xử lý khi bấm nút "Gửi file nhạc MP3"
    if update.callback_query:
        query = update.callback_query
        await query.answer("⏳ Đang trích xuất nhạc MP3...")

        if query.data and query.data.startswith("mp3|"):
            video_id = query.data.split("|", 1)[1]
            tiktok_url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
            video_data, _ = await fetch_tiktok_data(tiktok_url)

            if video_data and video_data.get("music"):
                music_url = video_data["music"]
                music_title = video_data.get("music_info", {}).get(
                    "title", "TikTok Audio"
                )
                await bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=music_url,
                    title=music_title,
                    caption="🎵 Nhạc nền gốc từ TikTok",
                )
            else:
                await bot.send_message(
                    chat_id=query.message.chat_id,
                    text="❌ Không thể tải file nhạc cho bài đăng này.",
                )
        return {"ok": True}

    # 2. Xử lý tin nhắn văn bản / link TikTok
    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if text.startswith("/start") or text.startswith("/help"):
        welcome_msg = (
            "👋 **Chào mừng bạn đến với Bot Tải Video & Ảnh TikTok Không Logo!**\n\n"
            "📌 **Tính năng hỗ trợ:**\n"
            "• Tải video TikTok HD không dính watermark\n"
            "• Tải toàn bộ bộ ảnh (Slideshow / Photo Mode)\n"
            "• Tách riêng file nhạc nền MP3\n\n"
            "👉 Chỉ cần dán link TikTok vào đây để bắt đầu!"
        )
        await bot.send_message(
            chat_id=chat_id, text=welcome_msg, parse_mode="Markdown"
        )
        return {"ok": True}

    match = TIKTOK_URL_PATTERN.search(text)
    if not match:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Vui lòng gửi đường link TikTok hợp lệ.",
        )
        return {"ok": True}

    tiktok_url = match.group(1)
    status_msg = await bot.send_message(
        chat_id=chat_id, text="⏳ Đang xử lý dữ liệu từ TikTok..."
    )

    media_data, error_msg = await fetch_tiktok_data(tiktok_url)
    if not media_data:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ Không thể xử lý link này ({error_msg}).",
        )
        return {"ok": True}

    video_id = media_data.get("id")
    title = media_data.get("title") or "Bài đăng TikTok"
    author = media_data.get("author", {}).get("nickname", "Unknown")
    short_title = (title[:100] + "...") if len(title) > 100 else title
    caption = f"🎬 **{short_title}**\n👤 Tác giả: `{author}`"

    reply_markup = None
    if video_id and media_data.get("music"):
        buttons = [
            [
                InlineKeyboardButton(
                    "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

    # TRƯỜNG HỢP A: Bài đăng dạng Slideshow nhiều ảnh (Photo Mode)
    images = media_data.get("images")
    if images and isinstance(images, list) and len(images) > 0:
        try:
            # Telegram cho phép tối đa 10 ảnh mỗi album (Media Group), chia thành từng cụm 10 ảnh
            for i in range(0, len(images), 10):
                chunk = images[i : i + 10]
                media_group = []
                for idx, img_url in enumerate(chunk):
                    if i == 0 and idx == 0:
                        media_group.append(
                            InputMediaPhoto(
                                media=img_url,
                                caption=f"🖼 **Bộ {len(images)} ảnh** — {short_title}\n👤 Tác giả: `{author}`",
                                parse_mode="Markdown",
                            )
                        )
                    else:
                        media_group.append(InputMediaPhoto(media=img_url))

                await bot.send_media_group(chat_id=chat_id, media=media_group)

            # Gửi thêm nút tải nhạc nền của slideshow (nếu có)
            if reply_markup:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Đã tải xong **{len(images)}** ảnh không logo!",
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )
            await bot.delete_message(
                chat_id=chat_id, message_id=status_msg.message_id
            )
        except Exception as e:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Lỗi khi gửi bộ ảnh: {str(e)[:100]}",
            )
        return {"ok": True}

    # TRƯỜNG HỢP B: Bài đăng Video thông thường
    hd_url = media_data.get("hdplay") or media_data.get("play")
    standard_url = media_data.get("play")

    try:
        await bot.send_video(
            chat_id=chat_id,
            video=hd_url,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        await bot.delete_message(
            chat_id=chat_id, message_id=status_msg.message_id
        )
    except Exception:
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=standard_url,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            await bot.delete_message(
                chat_id=chat_id, message_id=status_msg.message_id
            )
        except Exception:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text="❌ Video này quá nặng (vượt giới hạn 50MB của Telegram) nên bot không thể gửi trực tiếp.",
            )

    return {"ok": True}
