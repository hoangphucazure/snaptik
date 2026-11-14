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

# Regex nhận diện link TikTok và Facebook
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

TELEGRAM_MAX_BYTES = 48 * 1024 * 1024


def normalize_media_url(url: str | None) -> str | None:
    """Đảm bảo link media luôn có đầy đủ giao thức https://"""
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.tikwm.com" + url
    return url


async def delete_user_message_safe(chat_id: int, message_id: int) -> None:
    """Tự động xóa tin nhắn chứa link của người dùng."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def marquee_status_animation(
    chat_id: int, message_id: int, label: str = "Đang xử lí..."
) -> None:
    """Tạo hiệu ứng chữ chạy ngang như bảng hiệu LED trong lúc chờ xử lý."""
    banner = f"⏳ {label}   "
    idx = 0
    try:
        while True:
            await asyncio.sleep(0.4)
            idx = (idx + 2) % len(banner)
            scrolled_text = banner[idx:] + banner[:idx]
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"`{scrolled_text}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def resolve_redirect_url(session: aiohttp.ClientSession, url: str) -> str:
    """Mở rộng link rút gọn (vt.tiktok.com, fb.watch, facebook.com/share/...) thành link gốc."""
    try:
        async with session.get(
            url, headers=HEADERS, allow_redirects=True, timeout=12
        ) as resp:
            return str(resp.url)
    except Exception:
        return url


async def get_file_size(url: str | None) -> int:
    """Kiểm tra dung lượng file (bytes) trước khi gửi sang Telegram."""
    if not url:
        return 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                url, headers=HEADERS, allow_redirects=True, timeout=8
            ) as resp:
                return int(resp.headers.get("Content-Length", 0))
    except Exception:
        return 0


def _clean_fb_url(raw: str) -> str:
    """Giải mã các ký tự escape JSON/HTML trong link video Facebook."""
    cleaned = raw.replace("\\/", "/").replace("\\u0025", "%")
    try:
        cleaned = json.loads(f'"{cleaned}"')
    except Exception:
        pass
    return html.unescape(cleaned)


async def fetch_facebook_data(fb_url: str) -> tuple[dict | None, str]:
    """Trích xuất link video HD/SD trực tiếp từ bài đăng Facebook công khai."""
    fb_headers = {
        **HEADERS,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    async with aiohttp.ClientSession() as session:
        resolved_url = await resolve_redirect_url(session, fb_url)
        clean_url = resolved_url.split("?")[0] if "/reel/" in resolved_url else resolved_url

        html_sources = []
        # Thử lấy HTML từ trang gốc và trang nhúng video plugin của Facebook
        target_pages = [
            clean_url,
            f"https://www.facebook.com/plugins/video.php?href={clean_url}",
        ]

        for page_url in target_pages:
            try:
                async with session.get(
                    page_url, headers=fb_headers, allow_redirects=True, timeout=15
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="ignore")
                        html_sources.append(text)
            except Exception:
                continue

        hd_url = None
        sd_url = None
        title = "Video Facebook"

        for page_html in html_sources:
            if not hd_url:
                for pattern in [
                    r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
                    r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
                    r'"hd_src"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, page_html)
                    if m:
                        hd_url = _clean_fb_url(m.group(1))
                        break

            if not sd_url:
                for pattern in [
                    r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
                    r'"playable_url"\s*:\s*"([^"]+)"',
                    r'"sd_src"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, page_html)
                    if m:
                        sd_url = _clean_fb_url(m.group(1))
                        break

            title_match = re.search(r'<title[^>]*>(.*?)</title>', page_html, re.S)
            if title_match:
                raw_title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
                if raw_title and "facebook" not in raw_title.lower():
                    title = raw_title

            if hd_url or sd_url:
                break

        if not hd_url and not sd_url:
            return (
                None,
                "Không tìm thấy luồng video. Video có thể ở chế độ riêng tư/nhóm kín hoặc link không hợp lệ.",
            )

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
    """Gọi API TikWM lấy dữ liệu video hoặc slideshow ảnh TikTok."""
    api_url = "https://www.tikwm.com/api/"

    async with aiohttp.ClientSession() as session:
        resolved = await resolve_redirect_url(session, tiktok_url)
        clean_url = resolved.split("?")[0]
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
                        data = result["data"]
                        data["platform"] = "TikTok"
                        return data, ""
                    if "limit" in str(result.get("msg", "")).lower():
                        await asyncio.sleep(1.2)
                        continue
                    return None, result.get("msg", "Không lấy được dữ liệu.")
            except Exception:
                await asyncio.sleep(1.2)

    return None, "Máy chủ đang bận, vui lòng thử lại sau vài giây."


@app.get("/{full_path:path}")
async def health_check(full_path: str = ""):
    return {"status": "TikTok & Facebook Downloader Bot is running!"}


@app.post("/{full_path:path}")
async def telegram_webhook(request: Request, full_path: str = ""):
    data = await request.json()
    update = Update.de_json(data, bot)

    # 1. Xử lý nút bấm gửi file nhạc MP3 trực tiếp (TikTok)
    if update.callback_query:
        query = update.callback_query
        await query.answer("⏳ Đang trích xuất nhạc MP3...")

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
                        text=f"🎵 File nhạc quá lớn (>50MB), tải trực tiếp tại đây:\n{music_url}",
                    )
            else:
                await bot.send_message(
                    chat_id=query.message.chat_id,
                    text="❌ Không thể tải file nhạc cho bài đăng này.",
                )
        return {"ok": True}

    # 2. Xử lý tin nhắn văn bản
    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat_id
    user_msg_id = update.message.message_id
    text = update.message.text.strip()

    if text.startswith("/start") or text.startswith("/help"):
        welcome_msg = (
            "👋 **Bot Tải Video TikTok & Facebook**\n\n"
            "• **TikTok:** Tải video HD không logo, tải bộ ảnh (Slideshow) & tách nhạc MP3.\n"
            "• **Facebook:** Hỗ trợ link Reels, Watch, `fb.watch`, video bài đăng công khai.\n"
            "• Bot của Nguyễn Hoàng Phúc \n\n"
            "👉 Dán link TikTok hoặc Facebook vào đây để bắt đầu!"
        )
        await bot.send_message(
            chat_id=chat_id, text=welcome_msg, parse_mode="Markdown"
        )
        return {"ok": True}

    # Phát hiện nền tảng từ link người dùng gửi
    tiktok_match = TIKTOK_URL_PATTERN.search(text)
    fb_match = FACEBOOK_URL_PATTERN.search(text)

    if not tiktok_match and not fb_match:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Vui lòng gửi đường link **TikTok** hoặc **Facebook** hợp lệ.",
            parse_mode="Markdown",
        )
        return {"ok": True}

    platform_label = "Đang tải TikTok..." if tiktok_match else "Đang tải Facebook..."
    status_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"`⏳ {platform_label}   `",
        parse_mode="Markdown",
    )
    anim_task = asyncio.create_task(
        marquee_status_animation(chat_id, status_msg.message_id, platform_label)
    )

    try:
        if tiktok_match:
            media_data, error_msg = await fetch_tiktok_data(tiktok_match.group(1))
        else:
            media_data, error_msg = await fetch_facebook_data(fb_match.group(1))

        if not media_data:
            anim_task.cancel()
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Không thể xử lý link này ({error_msg}).",
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

        # TRƯỜNG HỢP A: Bài đăng nhiều ảnh (Slideshow TikTok)
        images = media_data.get("images")
        if images and isinstance(images, list) and len(images) > 0:
            try:
                for i in range(0, len(images), 10):
                    chunk = images[i : i + 10]
                    media_group = []
                    for idx, raw_img_url in enumerate(chunk):
                        img_url = normalize_media_url(raw_img_url)
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

                if video_id and media_data.get("music"):
                    btn = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🎵 Gửi file nhạc MP3",
                                    callback_data=f"mp3|{video_id}",
                                )
                            ]
                        ]
                    )
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Đã tải xong **{len(images)}** ảnh không logo!",
                        parse_mode="Markdown",
                        reply_markup=btn,
                    )
                anim_task.cancel()
                await bot.delete_message(
                    chat_id=chat_id, message_id=status_msg.message_id
                )
                await delete_user_message_safe(chat_id, user_msg_id)
            except Exception as e:
                anim_task.cancel()
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=f"❌ Lỗi khi gửi bộ ảnh: {str(e)[:100]}",
                )
            return {"ok": True}

        # TRƯỜNG HỢP B: Bài đăng Video (TikTok hoặc Facebook)
        hd_url = normalize_media_url(media_data.get("hdplay"))
        sd_url = normalize_media_url(media_data.get("play"))

        hd_size = await get_file_size(hd_url)
        sd_size = await get_file_size(sd_url)

        buttons = []
        if video_id and media_data.get("music"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                    )
                ]
            )

        target_url = None
        quality_note = ""

        if hd_url and (0 < hd_size <= TELEGRAM_MAX_BYTES):
            target_url = hd_url
        elif sd_url and (0 < sd_size <= TELEGRAM_MAX_BYTES):
            target_url = sd_url
            if hd_size > TELEGRAM_MAX_BYTES:
                size_mb = round(hd_size / (1024 * 1024), 1)
                quality_note = f"\nℹ️ *Bản HD gốc nặng {size_mb}MB (>50MB) nên bot gửi bản tiêu chuẩn để phát trực tiếp.*"
                buttons.insert(
                    0,
                    [
                        InlineKeyboardButton(
                            f"📥 Tải bản HD gốc ({size_mb}MB)", url=hd_url
                        )
                    ],
                )
        elif hd_size == 0 and sd_size == 0:
            target_url = hd_url or sd_url

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

        if target_url:
            try:
                await bot.send_video(
                    chat_id=chat_id,
                    video=target_url,
                    caption=caption + quality_note,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )
                anim_task.cancel()
                await bot.delete_message(
                    chat_id=chat_id, message_id=status_msg.message_id
                )
                await delete_user_message_safe(chat_id, user_msg_id)
                return {"ok": True}
            except Exception:
                # Dự phòng: Nếu gửi bản HD thất bại (ví dụ do vượt dung lượng khi server không báo size), thử lại bằng bản SD
                if sd_url and target_url != sd_url:
                    try:
                        await bot.send_video(
                            chat_id=chat_id,
                            video=sd_url,
                            caption=caption,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                        )
                        anim_task.cancel()
                        await bot.delete_message(
                            chat_id=chat_id, message_id=status_msg.message_id
                        )
                        await delete_user_message_safe(chat_id, user_msg_id)
                        return {"ok": True}
                    except Exception:
                        pass

        # TRƯỜNG HỢP C: Video > 50MB -> Cấp link tải thẳng
        best_url = hd_url or sd_url
        final_size_mb = round(max(hd_size, sd_size) / (1024 * 1024), 1)
        size_text = f" ({final_size_mb}MB)" if final_size_mb > 0 else " (>50MB)"

        fallback_buttons = [
            [InlineKeyboardButton(f"⬇️ Tải Video Gốc{size_text}", url=best_url)]
        ]
        if video_id and media_data.get("music"):
            fallback_buttons.append(
                [
                    InlineKeyboardButton(
                        "🎵 Gửi file nhạc MP3", callback_data=f"mp3|{video_id}"
                    )
                ]
            )

        anim_task.cancel()
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=(
                f"{caption}\n\n"
                f"⚠️ **Video vượt quá giới hạn 50MB của Telegram{size_text}.**\n"
                f"Bấm nút bên dưới để tải trực tiếp file gốc về máy:"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(fallback_buttons),
        )
        await delete_user_message_safe(chat_id, user_msg_id)

    finally:
        if not anim_task.done():
            anim_task.cancel()

    return {"ok": True}
