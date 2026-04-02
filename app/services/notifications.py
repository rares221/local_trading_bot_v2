"""
Notification service: send alerts via webhook (Discord, Slack, Telegram, or generic).
Supports: batch ready, regime change, macro calendar alerts.
"""
import logging
from typing import Any, Optional
import httpx

from app.core.preferences import _load_prefs, _save_prefs

logger = logging.getLogger(__name__)

# Notification event types
EVENT_BATCH_READY = "batch_ready"
EVENT_REGIME_CHANGE = "regime_change"
EVENT_MACRO_ALERT = "macro_alert"
EVENT_TRADE_OUTCOME = "trade_outcome"


def get_webhook_config() -> dict[str, Any]:
    """Get webhook notification settings from preferences."""
    data = _load_prefs()
    return {
        "webhook_url": data.get("webhook_url") or "",
        "webhook_enabled": data.get("webhook_enabled", False),
        "notify_batch_ready": data.get("notify_batch_ready", True),
        "notify_regime_change": data.get("notify_regime_change", True),
        "notify_macro_alert": data.get("notify_macro_alert", False),
        "notify_trade_outcome": data.get("notify_trade_outcome", False),
    }


def set_webhook_config(
    webhook_url: Optional[str] = None,
    webhook_enabled: Optional[bool] = None,
    notify_batch_ready: Optional[bool] = None,
    notify_regime_change: Optional[bool] = None,
    notify_macro_alert: Optional[bool] = None,
    notify_trade_outcome: Optional[bool] = None,
) -> dict[str, Any]:
    """Update webhook notification settings. Returns current config."""
    data = _load_prefs()
    if webhook_url is not None:
        data["webhook_url"] = webhook_url.strip()
    if webhook_enabled is not None:
        data["webhook_enabled"] = bool(webhook_enabled)
    if notify_batch_ready is not None:
        data["notify_batch_ready"] = bool(notify_batch_ready)
    if notify_regime_change is not None:
        data["notify_regime_change"] = bool(notify_regime_change)
    if notify_macro_alert is not None:
        data["notify_macro_alert"] = bool(notify_macro_alert)
    if notify_trade_outcome is not None:
        data["notify_trade_outcome"] = bool(notify_trade_outcome)
    _save_prefs(data)
    return get_webhook_config()


def _should_notify(event_type: str) -> bool:
    """Check if notifications are enabled for this event type."""
    config = get_webhook_config()
    if not config["webhook_enabled"] or not config["webhook_url"]:
        return False
    
    if event_type == EVENT_BATCH_READY:
        return config["notify_batch_ready"]
    if event_type == EVENT_REGIME_CHANGE:
        return config["notify_regime_change"]
    if event_type == EVENT_MACRO_ALERT:
        return config["notify_macro_alert"]
    if event_type == EVENT_TRADE_OUTCOME:
        return config["notify_trade_outcome"]
    return False


def _format_message(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Format notification payload. Supports Discord, Slack, and generic webhooks.
    Returns a dict that works with most webhook formats.
    """
    if event_type == EVENT_BATCH_READY:
        title = "📊 Daily Batch Ready"
        desc = f"**{data.get('count', 0)}** new recommendations generated."
        if data.get("categories"):
            cats = data["categories"]
            desc += f"\n• Stocks: {cats.get('stocks', 0)}"
            desc += f"\n• Commodities: {cats.get('commodities', 0)}"
            desc += f"\n• Indices: {cats.get('indices', 0)}"
            desc += f"\n• Other: {cats.get('other', 0)}"
        color = 0x10B981  # green
    
    elif event_type == EVENT_REGIME_CHANGE:
        old_regime = data.get("old_regime", "unknown")
        new_regime = data.get("new_regime", "unknown")
        title = "⚠️ Regime Change"
        desc = f"Market regime changed: **{old_regime}** → **{new_regime}**"
        if data.get("flagged_count", 0) > 0:
            desc += f"\n\n**{data['flagged_count']}** active trades flagged for review."
        color = 0xF59E0B if new_regime == "cautious" else (0xEF4444 if new_regime == "risk_off" else 0x10B981)
    
    elif event_type == EVENT_MACRO_ALERT:
        title = "📅 Macro Event Alert"
        events = data.get("events", [])
        if events:
            desc = "Upcoming high-impact events:\n"
            for ev in events[:5]:
                desc += f"• **{ev.get('date', '?')}** ({ev.get('days_away', '?')} days)\n"
        else:
            desc = "Macro calendar check complete."
        color = 0x3B82F6  # blue
    
    elif event_type == EVENT_TRADE_OUTCOME:
        title = "💰 Trade Outcome"
        outcome = data.get("outcome", "unknown")
        asset = data.get("asset_id", "Unknown")
        emoji = "✅" if outcome == "hit_tp" else ("❌" if outcome == "hit_sl" else "⏱️")
        desc = f"{emoji} **{asset}**: {outcome.replace('_', ' ').title()}"
        if data.get("pnl_pct") is not None:
            desc += f" ({data['pnl_pct']:+.1f}%)"
        color = 0x10B981 if outcome == "hit_tp" else (0xEF4444 if outcome == "hit_sl" else 0x6B7280)
    
    else:
        title = "🔔 Notification"
        desc = str(data)
        color = 0x6B7280

    # Format for Discord (embeds) and Slack (blocks) compatibility
    return {
        # Discord format
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
        }],
        # Slack format
        "text": f"{title}\n{desc}",
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n{desc}"}
        }],
        # Generic format
        "title": title,
        "message": desc,
    }


async def send_notification(event_type: str, data: dict[str, Any]) -> bool:
    """
    Send a notification if enabled for this event type.
    Returns True if sent successfully, False otherwise.
    """
    if not _should_notify(event_type):
        logger.debug("[NOTIFY] Skipping %s notification (disabled)", event_type)
        return False
    
    config = get_webhook_config()
    url = config["webhook_url"]
    
    if not url:
        logger.warning("[NOTIFY] No webhook URL configured")
        return False
    
    payload = _format_message(event_type, data)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code < 300:
                logger.info("[NOTIFY] Sent %s notification successfully", event_type)
                return True
            else:
                logger.warning("[NOTIFY] Webhook returned %d: %s", resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        logger.error("[NOTIFY] Failed to send %s notification: %s", event_type, e)
        return False


def send_notification_sync(event_type: str, data: dict[str, Any]) -> bool:
    """Synchronous version for use in non-async contexts."""
    if not _should_notify(event_type):
        return False
    
    config = get_webhook_config()
    url = config["webhook_url"]
    
    if not url:
        return False
    
    payload = _format_message(event_type, data)
    
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code < 300:
                logger.info("[NOTIFY] Sent %s notification successfully", event_type)
                return True
            else:
                logger.warning("[NOTIFY] Webhook returned %d", resp.status_code)
                return False
    except Exception as e:
        logger.error("[NOTIFY] Failed to send %s notification: %s", event_type, e)
        return False
