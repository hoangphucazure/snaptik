import os
import re
import aiohttp
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)

# Regex nhận diện các dạng link TikTok (web, mobile, link rút gọn vm/vt)
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

    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat_id
    text = update.message.text.strip()

    # Xử lý lệnh /start và /help
    if text.startswith("/start") or text.startswith("/help"):
        welcome_msg = (
            "👋 **Chào mừng bạn đến với Bot Tải Video TikTok Không Logo!**\n\n"
            "📌 **Cách dùng:** Chỉ cần dán đường dẫn (link) video TikTok vào đây, "
            "tôi sẽ tự động xóa watermark và gửi lại video gốc cho bạn."
        )
        await bot.send_message(chat_id=chat_id, text=welcome_msg, parse_mode="Markdown")
        return {"ok": True}

    # Tìm link TikTok trong tin nhắn
    match = TIKTOK_URL_PATTERN.search(text)
    if not match:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Vui lòng gửi đường link video TikTok hợp lệ (ví dụ: `https://vt.tiktok.com/...`).",
            parse_mode="Markdown",
        )
        return {"ok": True}

    tiktok_url = match.group(1)
    status_msg = await bot.send_message(chat_id=chat_id, text="⏳ Đang xử lý và xóa logo video...")

    # Lấy thông tin video
    video_data = await fetch_tiktok_data(tiktok_url)
    if not video_data:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="❌ Không thể tải video này. Link có thể bị lỗi, video ở chế độ riêng tư hoặc dạng slideshow ảnh.",
        )
        return {"ok": True}

    # Trích xuất các đường link tải về
    no_watermark_url = video_data.get("play")       # Link video không logo chuẩn
    hd_url = video_data.get("hdplay") or no_watermark_url  # Link video HD không logo
    music_url = video_data.get("music")             # Link nhạc MP3 gốc
    title = video_data.get("title") or "Video TikTok"
    author = video_data.get("author", {}).get("nickname", "Unknown")

    # Cắt ngắn mô tả nếu quá dài
    short_title = (title[:120] + "...") if len(title) > 120 else title
    caption = f"🎬 **{short_title}**\n👤 Tác giả: `{author}`"

    # Tạo nút bấm tải trực tiếp trên trình duyệt (dành cho video nặng > 50MB)
    buttons = [[InlineKeyboardButton("📥 Tải Video HD (Không Logo)", url=hd_url)]]
    if music_url:
        buttons.append([InlineKeyboardButton("🎵 Tải Nhạc Nền (MP3)", url=music_url)])
    reply_markup = InlineKeyboardMarkup(buttons)

    try:
        # Gửi thẳng video vào khung chat Telegram (Telegram hỗ trợ gửi qua URL tối đa ~20-50MB)
        await bot.send_video(
            chat_id=chat_id,
            video=no_watermark_url,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        # Xóa tin nhắn "Đang xử lý..." sau khi gửi thành công
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        # Dự phòng: Nếu video quá nặng vượt giới hạn gửi file của Telegram, gửi kèm nút bấm tải trực tiếp
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"{caption}\n\n⚠️ Video có dung lượng lớn nên không thể phát trực tiếp trên Telegram. Vui lòng bấm nút bên dưới để tải về máy:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    return {"ok": True}