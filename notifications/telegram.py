"""Telegram notifications for trades and daily summaries."""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self._token = os.environ.get("TELEGRAM_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)
        self._bot = None

    async def _get_bot(self):
        if self._bot is not None:
            return self._bot
        if not self._enabled:
            return None
        try:
            from telegram import Bot
            self._bot = Bot(token=self._token)
            return self._bot
        except Exception as e:
            logger.error("Telegram bot init failed: %s", e)
            self._enabled = False
            return None

    async def send(self, message: str):
        bot = await self._get_bot()
        if bot is None:
            return
        try:
            await bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Telegram send error: %s", e)

    async def notify_trade(self, data: dict):
        """Send trade notification."""
        if not self._enabled:
            return

        if "outcome" in data:
            # Trade resolved
            outcome = data["outcome"].upper()
            pnl = data.get("pnl", 0)
            bankroll = data.get("bankroll", 0)
            if outcome == "TAKE_PROFIT":
                icon = "🎯"
            elif outcome == "STOP_LOSS":
                icon = "🛑"
            elif outcome == "WIN":
                icon = "✅"
            else:
                icon = "❌"
            msg = (
                f"{icon} <b>{outcome}</b>\n"
                f"P&L: <b>${pnl:+.2f}</b>\n"
                f"Bankroll: <b>${bankroll:.2f}</b>"
            )
        elif "side" in data:
            # New trade
            side = data["side"].upper()
            icon = "📈" if side == "UP" else "📉"
            signal = data.get("signal", 0)
            size = data.get("size_usd", 0)
            price = data.get("entry_price", 0)
            msg = (
                f"{icon} <b>NEW TRADE: {side}</b>\n"
                f"Signal: {signal:+.3f}\n"
                f"Size: ${size:.2f} @ {price:.2f}¢"
            )
        else:
            return

        await self.send(msg)

    async def daily_summary(self, stats: dict):
        """Send daily summary."""
        if not self._enabled:
            return

        trades = stats.get("trades_count", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        pnl = stats.get("pnl", 0)
        bankroll = stats.get("bankroll_close", 0)
        wr = wins / trades * 100 if trades > 0 else 0

        icon = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{icon} <b>DAILY SUMMARY</b>\n"
            f"Date: {stats.get('date', 'N/A')}\n"
            f"Trades: {trades} (W:{wins} L:{losses})\n"
            f"Win Rate: {wr:.1f}%\n"
            f"P&L: <b>${pnl:+.2f}</b>\n"
            f"Bankroll: <b>${bankroll:.2f}</b>"
        )
        await self.send(msg)
