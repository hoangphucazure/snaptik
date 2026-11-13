import os
import re
import aiohttp
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)

TIKTOK_URL_PATTERN = re.compile(
    r"(https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+)"
)


async def fetch_tiktok_data(tiktok_url: str) -> dict | None:
    """Gọi API TikWM để lấy dữ liệu video không watermark và nhạc MP3."""
    api_url = "https://www.tikwm.com/api/"
    payload = {"url": tiktok_url, "hd": 1}

    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, data=payload) as response:
            if response.status != 200:
                return None
            result = await response.json()
            if result.get("code") == 0 and result.get("data"):
                return result["data"]
            return None


@app.get("/{full_path:path}")
async def health_check(full_path: str = ""):
    return {"status": "TikTok Downloader Bot is running on Vercel!"}


@app.post("/{full_path:path}")
async def telegram_webhook(request: Request, full_path: str = ""):
    data = await request.json()
    update = Update.de_json(data, bot)

    # 1. Xử lý khi người dùng bấm nút "Tải Nhạc Nền (MP3)" ngay trong chat
    if update.callback_query:
        query = update.callback_query
        await query.answer("⏳ Đang gửi file nhạc MP3...")

        if query.data and query.data.startswith("mp3|"):
            video_id = query.data.split("|", 1)[1]
            tiktok_url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
            video_data = await fetch_tiktok_data(tiktok_url)

            if video_data and video_data.get("music"):
                music_url = video_data["music"]
                music_title = video_data.get("music_info", {}).get("title", "TikTok Audio")
                await bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=music_url,
                    title=music_title,
                    caption="🎵 Nhạc nền gốc từ video TikTok",
                )
            else:
                await bot.send_message(
                    chat_id=query.message.chat_id,
                    text="❌ Không thể trích xuất file nhạc cho video này.",
                )
        return {"ok": True}

    # 2. Xử lý khi người dùng gửi tin nhắn văn bản / link TikTok
    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if text.startswith("/start") or text.startswith("/help"):
        welcome_msg = (
            "👋 **Chào mừng bạn đến với Bot Tải Video TikTok Không Logo!**\n\n"
            "📌 **Cách dùng:** Dán link video TikTok vào đây, bot sẽ xử lý và gửi thẳng "
            "file video sạch vào khung chat để bạn lưu về máy."
        )
        await bot.send_message(chat_id=chat_id, text=welcome_msg, parse_mode="Markdown")
        return {"ok": True}

    match = TIKTOK_URL_PATTERN.search(text)
    if not match:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Vui lòng gửi đường link video TikTok hợp lệ.",
        )
        return {"ok": True}

    tiktok_url = match.group(1)
    status_msg = await bot.send_message(chat_id=chat_id, text="⏳ Đang tải và xóa logo video...")

    video_data = await fetch_tiktok_data(tiktok_url)
    if not video_data:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="❌ Không thể xử lý video này. Link có thể bị lỗi hoặc video ở chế độ riêng tư.",
        )
        return {"ok": True}

    # Ưu tiên lấy link HD không watermark, nếu không có thì lấy bản chuẩn
    hd_url = video_data.get("hdplay") or video_data.get("play")
    standard_url = video_data.get("play")
    video_id = video_data.get("id")
    title = video_data.get("title") or "Video TikTok"
    author = video_data.get("author", {}).get("nickname", "Unknown")

    short_title = (title[:100] + "...") if len(title) > 100 else title
    caption = f"🎬 **{short_title}**\n👤 Tác giả: `{author}`"

    # Tạo nút bấm xử lý nội bộ (callback_data) để gửi thẳng file MP3 vào chat
    reply_markup = None
    if video_id and video_data.get("music"):
        buttons = [[InlineKeyboardButton("🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}")]]
        reply_markup = InlineKeyboardMarkup(buttons)

    try:
        # Thử gửi thẳng bản HD vào khung chat
        await bot.send_video(
            chat_id=chat_id,
            video=hd_url,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        # Nếu bản HD quá nặng vượt mức cho phép của Telegram, tự động gửi bản chuẩn vào chat
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=standard_url,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text="❌ Video này quá dài/nặng (vượt giới hạn 50MB của Telegram) nên bot không thể gửi trực tiếp.",
            )

    return {"ok": True}
