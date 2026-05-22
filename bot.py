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
SEND_TIMEOUT = 600.0  # 10 minutes max for a single response

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("tg-claude-pty")

# ── Global state ─────────────────────────────────────────────────────────────

bridge: Optional[PtyBridge] = None
send_lock = asyncio.Lock()

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
    """Restart the bridge if Claude process has exited."""
    global bridge
    if bridge is None or not bridge.is_running:
        logger.info("Bridge not running, starting...")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: bridge.start(loop))
            logger.info("Bridge started successfully")
            return True
        except Exception as e:
            logger.error("Bridge start failed: %s", e)
            return False
    return True


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

    async with send_lock:
        await bridge.stop()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: bridge.start(loop))
            await msg.edit_text("✅ Claude session reset. Start a new conversation!")
        except Exception as e:
            logger.error("Failed to restart bridge: %s", e)
            await msg.edit_text("❌ Failed to restart Claude. Please try again later.")


async def stop_command(update: Update, _context):
    """Handle /stop command — restart Claude if stuck."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    global bridge
    msg = await update.message.reply_text("⏳ Restarting Claude...")

    async with send_lock:
        await bridge.stop()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: bridge.start(loop))
            await msg.edit_text("✅ Claude restarted.")
        except Exception as e:
            logger.error("Failed to restart bridge: %s", e)
            await msg.edit_text("❌ Failed to restart Claude. Please try again later.")


async def handle_message(update: Update, _context):
    """Handle incoming text/image messages — forward to Claude via PTY."""
    if not _is_authorized(update):
        return  # Silently ignore unauthorized users

    global bridge

    user_text = update.message.text.strip() if update.message.text else ""
    photo = update.message.photo

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

    await update.message.chat.send_action(action="typing")
    processing_msg = await update.message.reply_text("⏳ Processing...")

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

    # Send to Claude
    async with send_lock:
        # Ensure bridge is running
        if bridge is None or not bridge.is_ready:
            ok = await _ensure_bridge_running()
            if not ok:
                await processing_msg.edit_text(
                    "😔 Sorry, I can't connect to Claude. Please try again later."
                )
                if image_path:
                    try:
                        os.unlink(image_path)
                    except OSError:
                        pass
                return

        try:
            reply_text = await bridge.send(prompt, timeout=SEND_TIMEOUT)
        except Exception as e:
            logger.error("Error during bridge.send: %s\n%s", e, traceback.format_exc())
            reply_text = None
        finally:
            # Clean up temp image file
            if image_path:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass

    if reply_text is None:
        await processing_msg.edit_text(
            "😔 Sorry, Claude didn't respond. Please try again."
        )
        return

    reply_text = _truncate_reply(reply_text)
    logger.info("Reply text (%d chars): %s", len(reply_text), reply_text[:1000])
    formatted_text = _format_markdown_v2(reply_text)

    try:
        await processing_msg.edit_text(
            formatted_text, parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.warning("MarkdownV2 parse failed, falling back to plain text: %s", e)
        try:
            await processing_msg.edit_text(reply_text)
        except Exception as e2:
            logger.error("Plain text fallback also failed: %s", e2)
            await processing_msg.edit_text(
                "😔 Sorry, an error occurred formatting the reply."
            )


# ── Startup / Shutdown ──────────────────────────────────────────────────────


async def post_init(app: Application):
    """Start the PtyBridge after the event loop is running."""
    global bridge
    bridge = PtyBridge(claude_bin=CLAUDE_BIN, session_id=SESSION_ID)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: bridge.start(loop))
        logger.info("Claude Code PTY bridge ready")
    except Exception as e:
        logger.error("Failed to start PTY bridge: %s", e)
        raise


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
