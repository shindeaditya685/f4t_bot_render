"""Telegram control bot for the Free4Talk presence service."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from bot_manager import bot_manager
from models import Bot, BotCreate, BotStatus, now_iso
from store import BotStore

logger = logging.getLogger("telegram_bot")


def _env_list(name: str) -> set[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def _short_id(bot_id: str) -> str:
    return bot_id.split("-", 1)[0]


def _format_bot(bot: Bot) -> str:
    nickname = html.escape(bot.nickname)
    room_url = html.escape(bot.room_url)

    return (
        f"{nickname}\n"
        f"ID: <code>{bot.id}</code>\n"
        f"Status: <b>{bot.status.value}</b>\n"
        f"Logged in: {'yes' if bot.logged_in else 'no'}\n"
        f"Auto-start: {'yes' if bot.auto_start else 'no'}\n"
        f"Room: {room_url}"
    )


@dataclass
class TelegramControlBot:
    token: str
    store: BotStore
    allowed_chat_ids: set[str]
    poll_timeout: int = 30

    def __post_init__(self) -> None:
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self._offset = 0
        self._stop_event = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self.last_error = ""
        self.username = ""

    async def run(self) -> None:
        logger.info("Telegram bot polling started")
        timeout = aiohttp.ClientTimeout(total=self.poll_timeout + 10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            await self._prepare_polling()
            await self._send_startup_message()

            while not self._stop_event.is_set():
                try:
                    updates = await self._get_updates()
                    self.last_error = ""
                    for update in updates:
                        self._offset = max(self._offset, update["update_id"] + 1)
                        await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:500]
                    logger.warning("Telegram polling error: %s", exc)
                    await asyncio.sleep(5)

        self._session = None
        logger.info("Telegram bot polling stopped")

    def stop(self) -> None:
        self._stop_event.set()

    async def _get_updates(self) -> list[dict[str, Any]]:
        assert self._session is not None
        async with self._session.get(
            f"{self.api_base}/getUpdates",
            params={
                "offset": self._offset,
                "timeout": self.poll_timeout,
                "allowed_updates": '["message"]',
            },
        ) as response:
            payload = await response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)
            return payload.get("result", [])

    async def _call_api(self, method: str, **params: Any) -> dict[str, Any]:
        assert self._session is not None
        async with self._session.post(
            f"{self.api_base}/{method}",
            json=params or None,
        ) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {"ok": False, "description": await response.text()}

            if not payload.get("ok"):
                raise RuntimeError(
                    f"{method} failed: {payload.get('description', payload)}"
                )

            return payload

    async def _prepare_polling(self) -> None:
        me = await self._call_api("getMe")
        result = me.get("result") or {}
        self.username = result.get("username", "")
        logger.info(
            "Telegram bot connected as @%s",
            self.username or result.get("first_name", "unknown"),
        )

        # Long polling fails if a webhook is still configured for this token.
        await self._call_api("deleteWebhook", drop_pending_updates=False)

    async def _send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        disable_web_page_preview: bool = True,
    ) -> None:
        assert self._session is not None
        async with self._session.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            },
        ) as response:
            if response.status >= 400:
                logger.warning("Telegram send failed: %s", await response.text())

    async def _send_startup_message(self) -> None:
        if not self.allowed_chat_ids:
            return

        for chat_id in self.allowed_chat_ids:
            await self._send_message(chat_id, "Free4Talk control bot is online.")

    def _is_allowed(self, chat_id: int | str) -> bool:
        return not self.allowed_chat_ids or str(chat_id) in self.allowed_chat_ids

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if not chat_id or not text:
            return

        if not self._is_allowed(chat_id):
            await self._send_message(
                chat_id,
                "This chat is not allowed. Add this chat ID to "
                f"<code>TELEGRAM_ALLOWED_CHAT_IDS</code>: <code>{chat_id}</code>",
            )
            return

        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        handlers = {
            "/start": self._help,
            "/help": self._help,
            "/bots": self._bots,
            "/new": self._new,
            "/startbot": self._startbot,
            "/stopbot": self._stopbot,
            "/deletebot": self._deletebot,
            "/status": self._status,
            "/viewer": self._viewer,
        }

        handler = handlers.get(command)
        if not handler:
            await self._send_message(chat_id, "Unknown command. Send /help.")
            return

        await handler(chat_id, rest.strip())

    async def _help(self, chat_id: int | str, _rest: str = "") -> None:
        await self._send_message(
            chat_id,
            "\n".join(
                [
                    "<b>Free4Talk Control Bot</b>",
                    "",
                    "/bots - list rooms",
                    "/new nickname | https://www.free4talk.com/room/... - create and start",
                    "/startbot ID - start a bot",
                    "/stopbot ID - stop a bot",
                    "/status ID - show details",
                    "/viewer ID - get dashboard viewer link",
                    "/deletebot ID - delete bot and browser profile",
                    "",
                    "You can use the full ID or the short first part.",
                ]
            ),
        )

    async def _resolve_bot(self, value: str) -> Bot | None:
        needle = value.strip()
        if not needle:
            return None

        docs = await self.store.list_bots()
        for doc in docs:
            bot_id = str(doc.get("id", ""))
            if bot_id == needle or bot_id.startswith(needle):
                return Bot(**doc)
        return None

    async def _save_bot(self, bot: Bot) -> None:
        bot.updated_at = now_iso()
        await self.store.save_bot(bot.model_dump())

    async def _bots(self, chat_id: int | str, _rest: str = "") -> None:
        docs = await self.store.list_bots()
        if not docs:
            await self._send_message(chat_id, "No bots yet. Use /new to create one.")
            return

        lines = ["<b>Your bots</b>"]
        for doc in docs:
            bot = Bot(**doc)
            runtime = bot_manager.runtime_info(bot.id)
            status = runtime.get("status") or bot.status.value
            logged_in = runtime.get("logged_in", bot.logged_in)
            nickname = html.escape(bot.nickname)
            lines.append(
                f"\n<b>{nickname}</b> <code>{_short_id(bot.id)}</code>\n"
                f"{status} | logged in: {'yes' if logged_in else 'no'}"
            )

        await self._send_message(chat_id, "\n".join(lines))

    async def _new(self, chat_id: int | str, rest: str) -> None:
        match = re.match(r"(.+?)\s*\|\s*(https?://\S+)", rest)
        if not match:
            await self._send_message(
                chat_id,
                "Use: <code>/new nickname | https://www.free4talk.com/room/...</code>",
            )
            return

        payload = BotCreate(
            nickname=match.group(1).strip(),
            room_url=match.group(2).strip(),
            auto_start=True,
        )
        bot = Bot(**payload.model_dump())
        await self._save_bot(bot)

        await self._send_message(
            chat_id, f"Created <b>{html.escape(bot.nickname)}</b>. Starting..."
        )
        try:
            instance = await bot_manager.start_bot(bot.id, bot.nickname, bot.room_url)
            bot.status = BotStatus.STARTING
            bot.display_num = instance.display_num
            bot.vnc_port = instance.vnc_port
            bot.last_message = instance.last_message
            await self._save_bot(bot)
            await self._send_message(chat_id, _format_bot(bot))
        except Exception as exc:
            bot.status = BotStatus.ERROR
            bot.last_message = str(exc)[:250]
            await self._save_bot(bot)
            await self._send_message(chat_id, f"Start failed: <code>{bot.last_message}</code>")

    async def _startbot(self, chat_id: int | str, rest: str) -> None:
        bot = await self._resolve_bot(rest)
        if not bot:
            await self._send_message(chat_id, "Bot not found.")
            return

        await self._send_message(
            chat_id, f"Starting <b>{html.escape(bot.nickname)}</b>..."
        )
        try:
            instance = await bot_manager.start_bot(bot.id, bot.nickname, bot.room_url)
            bot.status = BotStatus.STARTING
            bot.display_num = instance.display_num
            bot.vnc_port = instance.vnc_port
            bot.last_message = instance.last_message
            await self._save_bot(bot)
            await self._send_message(chat_id, _format_bot(bot))
        except Exception as exc:
            bot.status = BotStatus.ERROR
            bot.last_message = str(exc)[:250]
            await self._save_bot(bot)
            await self._send_message(chat_id, f"Start failed: <code>{bot.last_message}</code>")

    async def _stopbot(self, chat_id: int | str, rest: str) -> None:
        bot = await self._resolve_bot(rest)
        if not bot:
            await self._send_message(chat_id, "Bot not found.")
            return

        await bot_manager.stop_bot(bot.id)
        bot.status = BotStatus.STOPPED
        bot.last_message = "Stopped from Telegram"
        await self._save_bot(bot)
        await self._send_message(
            chat_id, f"Stopped <b>{html.escape(bot.nickname)}</b>."
        )

    async def _deletebot(self, chat_id: int | str, rest: str) -> None:
        bot = await self._resolve_bot(rest)
        if not bot:
            await self._send_message(chat_id, "Bot not found.")
            return

        await bot_manager.delete_bot_data(bot.id)
        await self.store.delete_bot(bot.id)
        await self._send_message(
            chat_id, f"Deleted <b>{html.escape(bot.nickname)}</b>."
        )

    async def _status(self, chat_id: int | str, rest: str) -> None:
        bot = await self._resolve_bot(rest)
        if not bot:
            await self._send_message(chat_id, "Bot not found.")
            return

        runtime = bot_manager.runtime_info(bot.id)
        bot.status = BotStatus(runtime["status"])
        bot.last_message = runtime["last_message"] or bot.last_message
        bot.logged_in = runtime["logged_in"]
        await self._send_message(chat_id, _format_bot(bot))

    async def _viewer(self, chat_id: int | str, rest: str) -> None:
        bot = await self._resolve_bot(rest)
        if not bot:
            await self._send_message(chat_id, "Bot not found.")
            return

        base_url = _public_base_url()
        if not base_url:
            await self._send_message(
                chat_id,
                "Set <code>PUBLIC_BASE_URL</code> to your deployed app URL to use viewer links.",
            )
            return

        await self._send_message(
            chat_id,
            f"<b>{html.escape(bot.nickname)}</b> viewer:\n{base_url}/bots/{bot.id}",
            disable_web_page_preview=False,
        )


def create_telegram_control_bot(store: BotStore) -> TelegramControlBot | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None

    return TelegramControlBot(
        token=token,
        store=store,
        allowed_chat_ids=_env_list("TELEGRAM_ALLOWED_CHAT_IDS"),
    )
