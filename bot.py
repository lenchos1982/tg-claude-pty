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

from config import (
    TELEGRAM_BOT_TOKEN, CLAUDE_BIN, ALLOWED_USER_IDS, SESSION_ID,
    PROGRESS_INTERVAL, TASK_DEFAULT_TIMEOUT,
)
from pty_bridge import PtyBridge

# ── Constants ────────────────────────────────────────────────────────────────

MAX_REPLY_LENGTH = 4096
SEND_TIMEOUT = TASK_DEFAULT_TIMEOUT  # max seconds for a single response

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


def _split_into_chunks(text: str, max_len: int = MAX_REPLY_LENGTH) -> list[str]:
    """Split long text into chunks that fit Telegram's message length limit.

    Tries to break at clean boundaries in order of preference:
      1. Between code blocks (outside ``` fences)
      2. Paragraph (\\n\\n)
      3. Chinese sentence-ending punctuation (。！？)
      4. Newlines (\\n)
      5. Spaces
      6. Hard split (last resort)
    """
    if len(text) <= max_len:
        return [text]

    def _find_split(text: str, max_len: int) -> int:
        """Find the best split position within max_len."""
        candidates = []

        # Paragraph boundary (only outside code fences)
        p = text.rfind("\n\n", 0, max_len)
        if p > 0 and not _inside_code_fence(text[:p]):
            candidates.append((p, p + 2))

        # Chinese sentence ending
        for sep in ("。", "！", "？"):
            p = text.rfind(sep, 0, max_len)
            if p > 0:
                candidates.append((p + len(sep), 0))
                break

        # Newline (only outside code fences)
        p = text.rfind("\n", 0, max_len)
        if p > 0 and not _inside_code_fence(text[:p]):
            candidates.append((p, p + 1))

        # Space
        p = text.rfind(" ", 0, max_len)
        if p > 0:
            candidates.append((p, p + 1))

        if candidates:
            candidates.sort(reverse=True)
            split_pos, consume = candidates[0]
            return split_pos + consume

        return max_len

    def _inside_code_fence(prefix: str) -> bool:
        """Check if prefix ends inside an unclosed code fence."""
        count = prefix.count("```")
        return count % 2 == 1

    chunks: list[str] = []
    remaining = text
    while len(remaining) > 0:
        if len(remaining) <= max_len:
            # Check if remaining text starts inside a code fence (from a previous
            # chunk that was split before the closing ```). Prepend the fence marker
            # so Telegram renders the code block properly.
            if chunks and remaining.startswith("\n"):
                # We need the closing fence from the previous chunk
                last = chunks[-1]
                if last.count("```") % 2 == 1:
                    remaining = "```" + remaining
            chunks.append(remaining)
            break

        split_pos = _find_split(remaining, max_len)
        chunk = remaining[:split_pos].strip()
        if chunk:
            # If chunk has an unclosed code fence, add closing ``` to it
            # so Telegram markdown doesn't break
            if _inside_code_fence(chunk):
                chunk += "\n```"
            chunks.append(chunk)
        remaining = remaining[split_pos:].strip()
        if not remaining:
            break

    return chunks


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


async def cancel_command(update: Update, _context):
    """Handle /cancel command — cancel the currently running task."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    global bridge

    if bridge is None or not bridge.task_active:
        await update.message.reply_text("ℹ️ No task is currently running.")
        return

    msg = await update.message.reply_text("🛑 Cancelling task...")

    try:
        await asyncio.get_running_loop().run_in_executor(
            None, bridge.cancel_current_task
        )
        await msg.edit_text("🚫 Task cancelled.")
    except Exception as e:
        logger.error("Cancel failed: %s", e)
        await msg.edit_text("❌ Failed to cancel task. Try /stop to restart.")


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


async def _monitor_task_completion(incoming_msg: object, task_prompt: str):
    """
    Background coroutine that monitors task completion and sends results.

    Runs concurrently with bot polling. Checks bridge._task_completed
    periodically and sends the result back to the user when done.
    """
    global bridge

    task_msg = incoming_msg
    start_time = asyncio.get_running_loop().time()
    last_progress_update = 0.0

    while True:
        if bridge is None or not bridge._running:
            logger.error("Bridge died during task execution - attempting auto-restart")
            try:
                await task_msg.edit_text("🔄 Bridge connection lost, reconnecting...")
            except Exception:
                pass
            try:
                ok = await _restart_bridge()
                if ok and bridge is not None and bridge.is_ready:
                    logger.info("Bridge auto-restarted after task failure")
                    try:
                        await task_msg.edit_text(
                            "🔁 Bridge reconnected, but the previous task was lost.\n"
                            "Please re-send your request."
                        )
                    except Exception:
                        pass
                else:
                    logger.error("Bridge auto-restart after task failure FAILED")
                    try:
                        await task_msg.edit_text("❌ Bridge reconnection failed. Please use /stop to restart manually.")
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Bridge auto-restart raised exception: %s", e)
                try:
                    await task_msg.edit_text("❌ Bridge reconnection failed: %s. Try /stop manually." % str(e)[:100])
                except Exception:
                    pass
            return

        # Task completed
        if bridge._task_completed.is_set():
            elapsed = asyncio.get_running_loop().time() - start_time
            logger.info("Task completed after %.1fs", elapsed)

            try:
                result = bridge._task_result
                if not result:
                    result = bridge._extract_task_summary()

                if result and len(result.strip()) > 0:
                    chunks = _split_into_chunks(result)
                    # First chunk: edit the confirmation message
                    formatted_first = _format_markdown_v2(chunks[0])
                    try:
                        await task_msg.edit_text(
                            formatted_first, parse_mode=ParseMode.MARKDOWN_V2
                        )
                    except Exception as e:
                        logger.warning("MarkdownV2 parse failed for chunk 0: %s", e)
                        from output_parser import strip_ansi
                        fallback = strip_ansi(chunks[0])
                        fallback = fallback.replace(r"\_", "_").replace(r"\*", "*")
                        await task_msg.edit_text(fallback)

                    # Remaining chunks: send as new messages
                    for i, chunk in enumerate(chunks[1:], 1):
                        formatted = _format_markdown_v2(chunk)
                        try:
                            await task_msg.reply_text(
                                formatted, parse_mode=ParseMode.MARKDOWN_V2
                            )
                        except Exception as e:
                            logger.warning("MarkdownV2 parse failed for chunk %d: %s", i, e)
                            from output_parser import strip_ansi
                            fallback = strip_ansi(chunk)
                            fallback = fallback.replace(r"\_", "_").replace(r"\*", "*")
                            await task_msg.reply_text(fallback)
                else:
                    await task_msg.edit_text("✅ Task completed (empty result).")
            except Exception as e:
                logger.error("Error formatting task result: %s", e)
                await task_msg.edit_text("✅ Task completed (error formatting result).")

            try:
                bridge.set_task_mode(False)
            except Exception:
                pass
            return

        # Task cancelled
        if bridge._task_cancelled.is_set():
            logger.info("Task was cancelled by user")
            try:
                await task_msg.edit_text("🚫 Task cancelled.")
            except Exception:
                pass
            try:
                bridge.set_task_mode(False)
            except Exception:
                pass
            return

        # Timeout check
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed >= TASK_DEFAULT_TIMEOUT:
            logger.warning("Task timed out after %.0fs", elapsed)
            try:
                await task_msg.edit_text(
                    f"⏰ Task timed out after {TASK_DEFAULT_TIMEOUT:.0f}s.\n"
                    f"Try breaking the request into smaller parts or use /new."
                )
            except Exception:
                pass
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, bridge.cancel_current_task
                )
            except Exception:
                pass
            return

        # Progress update every PROGRESS_INTERVAL seconds
        if elapsed - last_progress_update >= PROGRESS_INTERVAL:
            last_progress_update = elapsed
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            progress_text = f"⏳ Task running... ({minutes}m{seconds:02d}s elapsed)"
            try:
                await task_msg.edit_text(progress_text)
            except Exception:
                pass

        await asyncio.sleep(1.0)


async def handle_message(update: Update, _context):
    """Handle incoming messages - submit as task to Claude."""
    if not _is_authorized(update):
        return

    user_text = update.message.text.strip() if update.message.text else ""
    photo = update.message.photo

    logger.info(
        "Incoming from user %s: %s",
        update.effective_user.id if update.effective_user else "?",
        user_text[:200] if user_text else "(image)",
    )

    # Check if task already running
    if bridge is not None and bridge.task_active:
        await update.message.reply_text(
            "⏳ A task is already running. Please wait for it to complete or use /cancel."
        )
        return

    # Handle images
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
        return

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

    # Clean up temp file
    if image_path:
        try:
            os.unlink(image_path)
        except OSError:
            pass

    # Ensure bridge is running
    ok = await _ensure_bridge_running()
    if not ok:
        await update.message.reply_text(
            "😔 橋接器未就緒，正在自動重連請稍後再試\n\n"
            "• Try /stop to restart Claude"
        )
        return

    # Acquire lock and dispatch task
    async with _request_lock:
        # Send immediate confirmation
        confirm_msg = await update.message.reply_text("✅ 任務已接收，將在背景執行")

        # Submit task (non-blocking) and start monitor in background
        bridge.send_task(prompt)
        asyncio.create_task(_monitor_task_completion(confirm_msg, prompt))


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
    app.add_handler(CommandHandler("cancel", cancel_command))
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
