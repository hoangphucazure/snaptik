import os
import re
import json
import html
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
FACEBOOK_URL_PATTERN = re.compile(
    r"(https?://(?:www\.|m\.|web\.|mbasic\.)?(?:facebook\.com|fb\.watch|fb\.com)/[^\s]+)"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Tái sử dụng 1 HTTP Session duy nhất trên Vercel để bỏ qua thời gian bắt tay SSL lặp lại
_http_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(connector=connector, headers=HEADERS)
    return _http_session


def normalize_media_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.tikwm.com" + url
    return url


async def delete_msg_safe(chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _clean_fb_url(raw: str) -> str:
    cleaned = raw.replace("\\/", "/").replace("\\u0025", "%")
    try:
        cleaned = json.loads(f'"{cleaned}"')
    except Exception:
        pass
    return html.unescape(cleaned)


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, allow_redirects=True, timeout=8) as resp:
            if resp.status == 200:
                return await resp.text(errors="ignore")
    except Exception:
        pass
    return ""


async def fetch_facebook_data(fb_url: str) -> tuple[dict | None, str]:
    """Tải song song trang gốc và plugin Facebook để rút ngắn một nửa thời gian."""
    session = get_session()

    # Giải nén nhanh nếu là link rút gọn fb.watch hoặc share
    target_url = fb_url
    if "fb.watch" in fb_url or "/share/" in fb_url:
        try:
            async with session.head(fb_url, allow_redirects=True, timeout=5) as resp:
                target_url = str(resp.url)
        except Exception:
            pass

    clean_url = target_url.split("?")[0] if "/reel/" in target_url else target_url
    plugin_url = f"https://www.facebook.com/plugins/video.php?href={clean_url}"

    # Bắn 2 request cùng lúc thay vì chờ tuần tự
    html_pages = await asyncio.gather(
        fetch_html(session, clean_url),
        fetch_html(session, plugin_url),
    )

    hd_url = None
    sd_url = None
    title = "Video Facebook"

    for page_html in html_pages:
        if not page_html:
            continue
        if not hd_url:
            for pattern in (
                r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
                r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
                r'"hd_src"\s*:\s*"([^"]+)"',
            ):
                m = re.search(pattern, page_html)
                if m:
                    hd_url = _clean_fb_url(m.group(1))
                    break

        if not sd_url:
            for pattern in (
                r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
                r'"playable_url"\s*:\s*"([^"]+)"',
                r'"sd_src"\s*:\s*"([^"]+)"',
            ):
                m = re.search(pattern, page_html)
                if m:
                    sd_url = _clean_fb_url(m.group(1))
                    break

        if title == "Video Facebook":
            title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S)
            if title_match:
                raw_title = html.unescape(
                    re.sub(r"<[^>]+>", "", title_match.group(1))
                ).strip()
                if raw_title and "facebook" not in raw_title.lower():
                    title = raw_title

        if hd_url and sd_url:
            break

    if not hd_url and not sd_url:
        return None, "Video ở chế độ riêng tư hoặc link không hỗ trợ."

    return {
        "platform": "Facebook",
        "title": title,
        "author": "Facebook Video",
        "hdplay": hd_url,
        "play": sd_url or hd_url,
        "id": None,
        "music": None,
        "images": None,
    }, ""


async def fetch_tiktok_data(tiktok_url: str) -> tuple[dict | None, str]:
    """Gửi thẳng link sang TikWM (TikWM tự xử lý link rút gọn vt/vm nhanh hơn server tự giải)."""
    session = get_session()
    api_url = "https://www.tikwm.com/api/"
    payload = {"url": tiktok_url, "hd": 1}

    for _ in range(2):
        try:
            async with session.post(api_url, data=payload, timeout=10) as response:
                if response.status != 200:
                    await asyncio.sleep(0.8)
                    continue
                result = await response.json()
                if result.get("code") == 0 and result.get("data"):
                    data = result["data"]
                    data["platform"] = "TikTok"
                    return data, ""
                if "limit" in str(result.get("msg", "")).lower():
                    await asyncio.sleep(1.0)
                    continue
                return None, result.get("msg", "Không lấy được dữ liệu.")
        except Exception:
            await asyncio.sleep(0.5)

    return None, "Máy chủ đang bận, vui lòng thử lại."


@app.get("/{full_path:path}")
async def health_check(full_path: str = ""):
    return {"status": "Fast TikTok & Facebook Bot is running!"}


@app.post("/{full_path:path}")
async def telegram_webhook(request: Request, full_path: str = ""):
    data = await request.json()
    update = Update.de_json(data, bot)

    # 1. Xử lý nút tải nhạc MP3
    if update.callback_query:
        query = update.callback_query
        await query.answer("⏳ Đang gửi MP3...")

        if query.data and query.data.startswith("mp3|"):
            video_id = query.data.split("|", 1)[1]
            tiktok_url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
            video_data, _ = await fetch_tiktok_data(tiktok_url)

            music_url = (
                normalize_media_url(video_data.get("music")) if video_data else None
            )
            if music_url:
                music_title = video_data.get("music_info", {}).get(
                    "title", "TikTok Audio"
                )
                try:
                    await bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=music_url,
                        title=music_title,
                        caption="🎵 Nhạc nền gốc từ TikTok",
                    )
                except Exception:
                    await bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"🎵 Tải file nhạc trực tiếp:\n{music_url}",
                    )
        return {"ok": True}

    # 2. Xử lý tin nhắn văn bản
    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat_id
    user_msg_id = update.message.message_id
    text = update.message.text.strip()

    if text.startswith("/start") or text.startswith("/help"):
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚡ **Bot Tải Video TikTok & Facebook Tốc Độ Cao**\n\n"
                "Dán link **TikTok** (video/ảnh) hoặc **Facebook** (Reels/Watch) vào đây để tải ngay!"
            ),
            parse_mode="Markdown",
        )
        return {"ok": True}

    tiktok_match = TIKTOK_URL_PATTERN.search(text)
    fb_match = FACEBOOK_URL_PATTERN.search(text)

    if not tiktok_match and not fb_match:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Vui lòng gửi link **TikTok** hoặc **Facebook** hợp lệ.",
            parse_mode="Markdown",
        )
        return {"ok": True}

    # Hiển thị trạng thái "đang gửi video..." trên thanh tiêu đề của Telegram (không tốn request)
    asyncio.create_task(bot.send_chat_action(chat_id=chat_id, action="upload_video"))
    status_msg = await bot.send_message(chat_id=chat_id, text="⚡ Đang tải...")

    if tiktok_match:
        media_data, error_msg = await fetch_tiktok_data(tiktok_match.group(1))
    else:
        media_data, error_msg = await fetch_facebook_data(fb_match.group(1))

    if not media_data:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ Lỗi: {error_msg}",
        )
        return {"ok": True}

    platform = media_data.get("platform", "Video")
    video_id = media_data.get("id")
    title = media_data.get("title") or f"Video {platform}"
    raw_author = media_data.get("author")
    author = (
        raw_author.get("nickname", "Unknown")
        if isinstance(raw_author, dict)
        else (raw_author or platform)
    )

    short_title = (title[:100] + "...") if len(title) > 100 else title
    caption = f"🎬 **[{platform}]** {short_title}\n👤 Nguồn: `{author}`"

    # TRƯỜNG HỢP A: Slideshow ảnh TikTok
    images = media_data.get("images")
    if images and isinstance(images, list) and len(images) > 0:
        try:
            for i in range(0, len(images), 10):
                chunk = images[i : i + 10]
                media_group = [
                    InputMediaPhoto(
                        media=normalize_media_url(img_url),
                        caption=(
                            f"🖼 **Bộ {len(images)} ảnh** — {short_title}\n👤 Tác giả: `{author}`"
                            if (i == 0 and idx == 0)
                            else None
                        ),
                        parse_mode="Markdown" if (i == 0 and idx == 0) else None,
                    )
                    for idx, img_url in enumerate(chunk)
                ]
                await bot.send_media_group(chat_id=chat_id, media=media_group)

            if video_id and media_data.get("music"):
                btn = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                            )
                        ]
                    ]
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Đã tải xong **{len(images)}** ảnh!",
                    parse_mode="Markdown",
                    reply_markup=btn,
                )

            # Dọn dẹp song song tin nhắn trạng thái và tin nhắn link của người dùng
            await asyncio.gather(
                delete_msg_safe(chat_id, status_msg.message_id),
                delete_msg_safe(chat_id, user_msg_id),
            )
        except Exception as e:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Lỗi gửi ảnh: {str(e)[:100]}",
            )
        return {"ok": True}

    # TRƯỜNG HỢP B: Video (Bắn thẳng bản HD -> Lỗi/quá 50MB tự lùi sang SD -> Vẫn >50MB cấp link tải)
    hd_url = normalize_media_url(media_data.get("hdplay"))
    sd_url = normalize_media_url(media_data.get("play"))

    buttons = []
    if video_id and media_data.get("music"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                )
            ]
        )
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    # Bước 1: Thử gửi thẳng bản HD (không cần đo dung lượng trước)
    if hd_url:
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=hd_url,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            await asyncio.gather(
                delete_msg_safe(chat_id, status_msg.message_id),
                delete_msg_safe(chat_id, user_msg_id),
            )
            return {"ok": True}
        except Exception:
            pass

    # Bước 2: Nếu bản HD vượt 50MB hoặc lỗi, tự động lùi sang bản SD + kèm nút tải HD gốc
    if sd_url and sd_url != hd_url:
        try:
            sd_buttons = list(buttons)
            if hd_url:
                sd_buttons.insert(
                    0, [InlineKeyboardButton("📥 Tải bản HD gốc (>50MB)", url=hd_url)]
                )
            await bot.send_video(
                chat_id=chat_id,
                video=sd_url,
                caption=caption + "\nℹ️ *Gửi bản tiêu chuẩn do bản HD vượt 50MB.*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(sd_buttons) if sd_buttons else None,
            )
            await asyncio.gather(
                delete_msg_safe(chat_id, status_msg.message_id),
                delete_msg_safe(chat_id, user_msg_id),
            )
            return {"ok": True}
        except Exception:
            pass

    # Bước 3: Cả 2 bản đều > 50MB -> Cấp link tải trực tiếp tốc độ cao
    best_url = hd_url or sd_url
    fallback_buttons = [[InlineKeyboardButton("⬇️ Tải Video Gốc (>50MB)", url=best_url)]]
    if video_id and media_data.get("music"):
        fallback_buttons.append(
            [
                InlineKeyboardButton(
                    "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                )
            ]
        )

    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text=f"{caption}\n\n⚠️ **Video vượt quá 50MB.** Bấm nút bên dưới để tải trực tiếp về máy:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(fallback_buttons),
    )
    await delete_msg_safe(chat_id, user_msg_id)

    return {"ok": True}
