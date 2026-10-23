#!/usr/bin/env python3
"""
Telegram Offline Auto-Responder (fixed)
---------------------------------------
Auto-replies to incoming messages ONLY while you are "offline".

Admin-only commands (ADMIN_ID):
  /offline  enable auto-replies
  /online   disable auto-replies
  /status   show current state

Everyone:
  /start    greeting
  /id       show your numeric user id (use this to set ADMIN_ID)

Install:
  pip install "python-telegram-bot[job-queue]==21.*"

Run:
  BOT_TOKEN=... ADMIN_ID=... python bot.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from pathlib import Path

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =====================================================================
#  CONFIGURE ME
# =====================================================================
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8535173611:AAFfFtkroy9tNzAsyT-QMFrNoAI8-ft3iqs"
).strip()

# Your numeric Telegram user ID. Send /id to the bot to find it.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8820459013") or 0)

OFFLINE_MESSAGE = (
    "🌷 <i><b>ゼインは現在オフラインです</b></i>\n\n"
    "<i>メッセージを残してください </i> 🩷🩷"
)

COOLDOWN_SECONDS = 2.0   # min gap between replies to the same person
TYPING_DELAY = 0.6       # fake "typing..." pause before replying
FORWARD_TO_ADMIN = True  # DM you a copy of every incoming message

# =====================================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("offline-responder")

STATE_FILE = Path(__file__).with_name("state.json")
NOPREVIEW = LinkPreviewOptions(is_disabled=True)
OWN_COMMANDS = {"start", "help", "id", "offline", "online", "status"}

_last_reply_at: dict[int, float] = {}


# ---------------------------------------------------------------------
#  state (survives restarts, validated)
# ---------------------------------------------------------------------
def load_state() -> dict:
    st = {"offline": True}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            st["offline"] = bool(data.get("offline", True))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.warning("state.json unreadable, using defaults")
    return st


def save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save state: %s", exc)


state = load_state()


# ---------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(ADMIN_ID) and u is not None and u.id == ADMIN_ID


async def reply(update: Update, text: str) -> None:
    msg = update.effective_message
    if msg is None:
        return
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NOPREVIEW)
    except TelegramError as exc:
        log.debug("reply failed: %s", exc)


def _slice_utf16(text: str, offset: int, length: int) -> str:
    """Telegram entity offsets are UTF-16 code units, not code points."""
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def is_addressed(msg, bot) -> bool:
    """True if bot was @mentioned, replied to, or a non-owned /command."""
    reply_to = msg.reply_to_message
    if reply_to and reply_to.from_user and reply_to.from_user.id == bot.id:
        return True

    text = msg.text or ""
    uname = (bot.username or "").lower()
    for ent in msg.entities or []:
        if ent.type == "mention":
            if uname and _slice_utf16(text, ent.offset, ent.length).lower() == f"@{uname}":
                return True
        elif ent.type == "text_mention" and ent.user and ent.user.id == bot.id:
            return True

    if text.startswith("/"):
        cmd = text[1:].split()[0].split("@")[0].lower()
        return cmd not in OWN_COMMANDS
    return False


# ---------------------------------------------------------------------
#  commands
# ---------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "Hi! I auto-reply while the owner is away. Send /id for your user id.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "/id — your user id\n/start — greeting")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        await reply(update, f"Your id: <code>{u.id}</code>")


async def cmd_offline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state["offline"] = True
    save_state()
    await reply(update, "🔴 Offline mode ON — auto-replies live.")


async def cmd_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state["offline"] = False
    save_state()
    await reply(update, "🟢 Offline mode OFF — staying quiet.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await reply(
        update,
        "🔴 Offline — auto-replying" if state["offline"] else "🟢 Online — silent",
    )


# ---------------------------------------------------------------------
#  the auto-reply
# ---------------------------------------------------------------------
async def auto_respond(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    if not state["offline"]:
        return
    if user.id == ADMIN_ID or user.is_bot:
        return
    if chat.type in ("group", "supergroup") and not is_addressed(msg, ctx.bot):
        return

    now = time.monotonic()
    if now - _last_reply_at.get(user.id, 0.0) < COOLDOWN_SECONDS:
        return
    _last_reply_at[user.id] = now
    if len(_last_reply_at) > 5000:
        cutoff = now - 3600
        for k in [k for k, v in _last_reply_at.items() if v < cutoff]:
            _last_reply_at.pop(k, None)

    preview = msg.text or msg.caption or "[media]"
    log.info("offline reply -> %s (%s) | %r", user.id, user.full_name, preview[:60])

    if FORWARD_TO_ADMIN and ADMIN_ID:
        name = html.escape(user.full_name or "unknown")
        uname = f" (@{html.escape(user.username)})" if user.username else ""
        where = html.escape(chat.title or chat.type)
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"📩 <b>{name}</b>{uname} · <i>{where}</i>\n"
                f"<code>{user.id}</code>\n\n{html.escape(preview)}",
                parse_mode=ParseMode.HTML,
                link_preview_options=NOPREVIEW,
            )
        except (Forbidden, BadRequest, TelegramError) as exc:
            log.debug("forward failed: %s", exc)

    try:
        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
        await asyncio.sleep(TYPING_DELAY)
    except TelegramError:
        pass

    try:
        if chat.type in ("group", "supergroup"):
            await ctx.bot.send_message(
                chat.id, OFFLINE_MESSAGE, parse_mode=ParseMode.HTML, link_preview_options=NOPREVIEW
            )
        else:
            await msg.reply_text(
                OFFLINE_MESSAGE, parse_mode=ParseMode.HTML, link_preview_options=NOPREVIEW
            )
    except TelegramError as exc:
        log.warning("send failed: %s", exc)


# ---------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------
async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    await app.bot.set_my_commands([
        BotCommand("start", "greeting"),
        BotCommand("id", "show your user id"),
        BotCommand("offline", "admin: enable auto-replies"),
        BotCommand("online", "admin: disable auto-replies"),
        BotCommand("status", "admin: show state"),
    ])
    log.info(
        "running as @%s (id=%s) — %s",
        me.username, me.id,
        "OFFLINE (auto-replying)" if state["offline"] else "ONLINE (silent)",
    )


def main() -> None:
    if ":" not in BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing/invalid")
    if not ADMIN_ID:
        log.warning("ADMIN_ID unset — admin commands disabled. Send /id to find yours.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("offline", cmd_offline))
    app.add_handler(CommandHandler("online", cmd_online))
    app.add_handler(CommandHandler("status", cmd_status))

    # group 1 -> runs only for updates no command handler in group 0 claimed
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, auto_respond), group=1
    )

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
