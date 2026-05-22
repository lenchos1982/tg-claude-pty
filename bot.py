#!/usr/bin/env python3
"""
Telegram ↔ Claude Code PTY Bridge — standalone, no acpx/OpenClaw dependency.

Architecture:
  Telegram BOT ←long polling→ bot.py
    └─ PtyBridge (PTY master fd)
         └─ /usr/bin/claude (full interactive CLI)

Supports: text conversations, session persistence, single-user whitelist.
"""

import asyncio
import logging
import os
import re
import sys
import tempfile
import traceback
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config import TELEGRAM_BOT_TOKEN, CLAUDE_BIN, ALLOWED_USER_IDS, SESSION_ID
from pty_bridge import PtyBridge

# ── Constants ────────────────────────────────────────────────────────────────

MAX_REPLY_LENGTH = 4000
SEND_TIMEOUT = 1800.0  # 30 minutes max for a single response

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("tg-claude-pty")

# ── Global state ─────────────────────────────────────────────────────────────

bridge: Optional[PtyBridge] = None

# ── Request Lock ─────────────────────────────────────────────────────────────
# Single-user: simple asyncio.Lock serializes requests.
# If lock is held, we return an immediate "please wait" message.
# No complex queue needed — there's no concurrency to manage.

_request_lock = asyncio.Lock()
_request_processor_task: Optional[asyncio.Task] = None

# ── Authorization ────────────────────────────────────────────────────────────


def _is_authorized(update: Update) -> bool:
    """Check if the user is authorized to use this bot."""
    user = update.effective_user
    if user is None:
        return False
    if not ALLOWED_USER_IDS:
        return True  # No whitelist = allow all
    if user.id in ALLOWED_USER_IDS:
        return True
    logger.warning(
        "Unauthorized access attempt by user %s (id=%s)",
        user.username or "?",
        user.id,
    )
    return False


# ── Helpers ──────────────────────────────────────────────────────────────────


def _escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special_chars = r"_*[]()~`>#+-=|{}.!"

    escaped = []
    i = 0
    while i < len(text):
        ch = text[i]

        # Preserve fenced code blocks
        if ch == "`" and text[i : i + 3] == "```":
            j = text.find("```", i + 3)
            if j != -1:
                escaped.append(text[i : j + 3])
                i = j + 3
                continue

        # Preserve inline code
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1:
                escaped.append(text[i : j + 1])
                i = j + 1
                continue

        if ch in special_chars:
            escaped.append("\\" + ch)
        else:
            escaped.append(ch)
        i += 1

    return "".join(escaped)


def _format_markdown_v2(text: str) -> str:
    """Convert Claude output to Telegram MarkdownV2-compatible text."""
    result = []
    parts = re.split(r"(```[\s\S]*?```|`[^`]+`)", text)

    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(part)
        else:
            result.append(_escape_markdown_v2(part))

    return "".join(result)


def _truncate_reply(text: str, max_len: int = MAX_REPLY_LENGTH) -> str:
    """Truncate reply to fit Telegram's message length limit."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_para = truncated.rfind("\n\n")
    if last_para > max_len // 2:
        truncated = truncated[:last_para]
    else:
        last_sentence = truncated.rfind("。")
        if last_sentence > max_len // 2:
            truncated = truncated[: last_sentence + 1]
        else:
            last_space = truncated.rfind(" ")
            if last_space > max_len // 2:
                truncated = truncated[:last_space]
    return truncated + "\n\n*(Truncated — response too long)*"


async def _ensure_bridge_running() -> bool:
    """
    Restart the bridge if Claude process has exited or is not ready.

    Performs a genuine health check:
      1. Checks flags (is_ready, is_running)
      2. Checks reader thread is_alive() and heartbeat
      3. Checks PTY fd validity
      4. If anything is wrong, fully resets the bridge
    """
    global bridge
    if bridge is None:
        logger.info("Bridge is None, starting...")
        return await _start_new_bridge()

    # Check flags first (fast path)
    if not bridge.is_ready or not bridge.is_running:
        logger.info("Bridge not ready (ready=%s, running=%s), restarting...",
                    bridge.is_ready, bridge.is_running)
        return await _restart_bridge()

    # Genuine health check: reader thread and PTY fd
    try:
        alive = bridge.reader_alive
    except Exception as e:
        logger.warning("Bridge reader health check failed: %s, restarting...", e)
        return await _restart_bridge()

    if not alive:
        logger.warning("Reader thread not healthy (reader_alive=False), restarting...")
        return await _restart_bridge()

    return True


async def _start_new_bridge() -> bool:
    """Create and start a new bridge instance."""
    global bridge
    bridge = PtyBridge(claude_bin=CLAUDE_BIN, session_id=SESSION_ID)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: bridge.start(loop))
        logger.info("New bridge started successfully")
        return True
    except Exception as e:
        logger.error("Bridge start failed: %s", e)
        return False


async def _restart_bridge() -> bool:
    """Restart the existing bridge (stop + start)."""
    global bridge
    if bridge is not None:
        try:
            await bridge.stop()
        except Exception as e:
            logger.warning("Bridge stop during restart had error: %s", e)
    return await _start_new_bridge()


# ── Handlers ─────────────────────────────────────────────────────────────────


async def start(update: Update, _context):
    """Handle /start command."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    user = update.effective_user
    welcome = (
        f"Hello {user.first_name or 'friend'}! 👋\n\n"
        "I'm a Telegram bridge to Claude Code CLI, powered by PTY.\n"
        "Send me a message and I'll forward it to Claude!\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/help - Usage instructions\n"
        "/new - Reset the conversation\n"
        "/stop - Restart Claude (if stuck)"
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, _context):
    """Handle /help command."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    help_text = (
        "🤖 *Usage*\n\n"
        "・Send text messages to chat with Claude\n"
        "・Send images for Claude to analyze\n"
        "・Conversation context is maintained\n\n"
        "Commands:\n"
        "`/start` - Show welcome message\n"
        "`/help` - Show this help\n"
        "`/new` - Reset conversation (fresh session)\n"
        "`/stop` - Restart Claude if it gets stuck"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)


async def new_command(update: Update, _context):
    """Handle /new command — stop and start a fresh Claude session."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    global bridge
    msg = await update.message.reply_text("🔄 Resetting Claude session...")

    # Lock the processor from picking up new requests while we reset
    await _pause_processor()
    try:
        if bridge is not None:
            await bridge.stop()
            bridge.reset()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: bridge.start(loop))
            await msg.edit_text("✅ Claude session reset. Start a new conversation!")
        except Exception as e:
            logger.error("Failed to restart bridge: %s", e)
            await msg.edit_text("❌ Failed to restart Claude. Please try again later.")
    finally:
        _resume_processor()


async def stop_command(update: Update, _context):
    """Handle /stop command — restart Claude if stuck."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    global bridge
    msg = await update.message.reply_text("⏳ Restarting Claude...")

    await _pause_processor()
    try:
        if bridge is not None:
            await bridge.stop()
            bridge.reset()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: bridge.start(loop))
            await msg.edit_text("✅ Claude restarted.")
        except Exception as e:
            logger.error("Failed to restart bridge: %s", e)
            await msg.edit_text("❌ Failed to restart Claude. Please try again later.")
    finally:
        _resume_processor()


# ── Request Processor ────────────────────────────────────────────────────────
# Single-user: asyncio.Lock serializes requests. If lock is held, the user
# gets an immediate "please wait" message. No queue, no drops.

_processor_paused = False  # Flag to temporarily pause processing (for /new, /stop)


def _resume_processor():
    """Resume the request processor after a pause."""
    global _processor_paused
    _processor_paused = False
    logger.info("Request processor resumed")


async def _pause_processor():
    """Pause the request processor temporarily."""
    global _processor_paused
    _processor_paused = True
    await asyncio.sleep(0.5)
    logger.info("Request processor paused")


async def _process_request_in_lock(
    prompt: str,
    image_path: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Process a single request inside the lock.
    Returns (result_text, error_message).
    """
    global bridge

    logger.info("Processing request (text_len=%d)", len(prompt))

    # Ensure bridge is running
    ok = await _ensure_bridge_running()
    if not ok:
        return None, "Bridge not available"

    # Send to Claude
    reply_text = None
    send_error = None
    try:
        reply_text = await bridge.send(prompt, timeout=SEND_TIMEOUT)
    except Exception as e:
        error_type = type(e).__name__
        logger.error("Error during bridge.send: [%s] %s\n%s", error_type, e, traceback.format_exc())
        send_error = error_type
        reply_text = None

    if reply_text is not None and len(reply_text) > 0:
        reply_text = _truncate_reply(reply_text)
        logger.info("Reply text (%d chars): %s", len(reply_text), reply_text[:1000])
        formatted_text = _format_markdown_v2(reply_text)
        return formatted_text, None

    elif reply_text is not None and len(reply_text) == 0:
        # Empty reply: try to extract something useful from the buffer
        logger.warning("Reply text is empty — checking buffer for fallback content")
        try:
            buffer_tail = bridge._get_readable_buffer_tail(max_chars=2000)
            if buffer_tail and len(buffer_tail) > 10:
                logger.info("Buffer fallback (%d chars): %s", len(buffer_tail), buffer_tail[:500])
                fallback_text = _truncate_reply(buffer_tail)
                formatted_text = _format_markdown_v2(fallback_text)
                return formatted_text + "\n\n_(⚠️ Task likely interrupted — content may be incomplete)_", None
            else:
                return None, "Claude 返回了空內容，請使用 /new 重置後再試"
        except Exception as buf_err:
            logger.error("Failed to get buffer fallback: %s", buf_err)
            return None, "Claude 返回了空內容（buffer 不可用）"

    else:
        if send_error:
            return None, f"系統錯誤：[{send_error}]，請重試或使用 /new 重置"
        else:
            return None, "Claude 沒有返回任何內容，可能是任務尚未完成"


async def handle_message(update: Update, _context):
    """Handle incoming text/image messages — forward to Claude via PTY."""
    if not _is_authorized(update):
        return  # Silently ignore unauthorized users

    user_text = update.message.text.strip() if update.message.text else ""
    photo = update.message.photo

    # Log incoming message content for debugging
    logger.info(
        "Incoming message from user %s: %s",
        update.effective_user.id if update.effective_user else "?",
        user_text[:200] if user_text else "(image)",
    )

    # Handle images: download to temp file
    image_path: Optional[str] = None
    if photo:
        file = await update.message.effective_attachment.get_file()
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(await file.download_as_bytearray())
        tmp.close()
        image_path = tmp.name
        logger.info("Image saved: %s (%d bytes)", image_path, os.path.getsize(image_path))
        if not user_text:
            user_text = "Please describe this image."

    if not user_text:
        return  # Not a text/image message

    # Build prompt with optional image
    prompt = user_text
    if image_path:
        import base64

        try:
            with open(image_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("ascii")
            prompt = (
                f"User sent an image:\n"
                f"data:image/jpeg;base64,{b64_data}\n\n"
                f"User message: {user_text}"
            )
        except OSError as e:
            logger.error("Failed to read image %s: %s", image_path, e)
            prompt = f"[Image attached]\nUser message: {user_text}"
            # Image will be cleaned up below even if we couldn't read it

    await update.message.chat.send_action(action="typing")

    # ── Try to acquire lock ──────────────────────────────────────────────
    if _request_lock.locked():
        logger.info("Lock already held — sending wait message")
        await update.message.reply_text(
            "⏳ A previous request is still being processed. Please wait..."
        )
        return

    processing_msg = await update.message.reply_text("⏳ Processing...")

    async with _request_lock:
        try:
            result_text, error_msg = await _process_request_in_lock(
                prompt=prompt,
                image_path=image_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("handle_message error: %s\n%s", e, traceback.format_exc())
            error_msg = f"系統錯誤：[{type(e).__name__}]，請重試或使用 /new 重置"
            result_text = None
        finally:
            # Clean up temp image file
            if image_path:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass

    # ── Handle result ────────────────────────────────────────────────────
    if result_text is not None:
        try:
            await processing_msg.edit_text(
                result_text, parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            logger.warning("MarkdownV2 parse failed, falling back to plain text: %s", e)
            from output_parser import strip_ansi
            fallback = strip_ansi(result_text)
            # Unescape markdown from the already-formatted result
            fallback = fallback.replace(r"\_", "_").replace(r"\*", "*")
            try:
                await processing_msg.edit_text(fallback)
            except Exception as e2:
                logger.error("Plain text fallback also failed: %s", e2)
                await processing_msg.edit_text(
                    "😔 Sorry, an error occurred formatting the reply."
                )
    else:
        logger.warning("Request failed: %s", error_msg)

        # Build a user-facing error message with specific detail
        if error_msg is None:
            reply = "😔 Sorry, Claude didn't respond."
        elif "Bridge not available" in str(error_msg):
            reply = (
                "😔 與 Claude 的連接異常，正在自動重連，請稍後重試\n\n"
                "• Try `/stop` to restart Claude\n"
                "• If this persists, the bridge may need admin attention"
            )
        elif "timeout" in str(error_msg).lower() or "timed out" in str(error_msg).lower():
            reply = (
                "⏰ Claude took too long to respond.\n\n"
                "• Try `/stop` to restart Claude\n"
                "• If the task is complex, break it into smaller parts"
            )
        elif "empty response" in str(error_msg).lower() or "空內容" in str(error_msg):
            reply = (
                "📭 Claude returned an empty response.\n\n"
                "• 請使用 /new 重置後再試"
            )
        elif "系統錯誤" in str(error_msg) or "Claude 沒有返回" in str(error_msg):
            reply = str(error_msg)
        else:
            reply = (
                f"😔 Sorry, Claude didn't respond.\n\n"
                f"{error_msg}"
            )
        try:
            await processing_msg.edit_text(reply)
        except Exception:
            pass


# ── Startup / Shutdown ──────────────────────────────────────────────────────


async def post_init(app: Application):
    """Start the PtyBridge after the event loop is running.

    If bridge startup fails, we mark it as degraded instead of raising.
    This prevents systemd from entering an infinite restart loop when
    Claude Code fails to start (e.g., auth issues, network problems).
    The _ensure_bridge_running function will retry on first user request.
    """
    global bridge, _request_processor_task

    bridge = PtyBridge(claude_bin=CLAUDE_BIN, session_id=SESSION_ID)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: bridge.start(loop))
        logger.info("Claude Code PTY bridge ready")
    except Exception as e:
        logger.error(
            "Failed to start PTY bridge: %s. Bot will run in degraded mode — "
            "bridge will be retried on first user request.", e
        )
        bridge._running = False
        bridge._ready = False


async def post_shutdown(app: Application):
    """Cleanly shut down the PTY bridge."""
    global bridge
    if bridge is not None:
        await bridge.stop()
        logger.info("Claude Code PTY bridge shut down")


async def error_handler(update: Update, context):
    """Handle errors from the telegram framework."""
    logger.error("Update %s caused error %s", update, context.error, exc_info=True)


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    """Build and run the bot application."""
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    app.add_error_handler(error_handler)

    logger.info(
        "tg-claude-pty started (claude_bin=%s, allowed_users=%s)",
        CLAUDE_BIN,
        sorted(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else "(all)",
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
