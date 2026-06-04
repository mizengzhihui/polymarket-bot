"""Feishu (Lark) webhook notification — shared across all bots.

Supports two delivery modes:
  1. Direct webhook (default)
  2. Relay proxy via US VPS (unblocked for Feishu API)
     Set env FEISHU_RELAY_URL to enable relay mode.
"""
import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)


def send_feishu(webhook_url: str, title: str, content_lines: list[str],
                color: str = "blue") -> bool:
    """Send an interactive card message to a Feishu bot.

    Args:
        webhook_url: Feishu webhook URL (empty string to skip).
                     If FEISHU_RELAY_URL is set, this is ignored and relay is used.
        title: Card header title (plain text).
        content_lines: Card body lines (Markdown via lark_md tag).
        color: Header color — blue, green, red, or yellow.

    Returns: True if sent successfully, False on failure.
    """
    import os
    relay_url = os.environ.get("FEISHU_RELAY_URL", "")

    if not webhook_url and not relay_url:
        logger.warning("send_feishu: no webhook URL or relay configured")
        return False

    # Build card elements (interactive card format)
    color_map = {"blue": "indigo", "green": "green",
                 "red": "red", "yellow": "yellow"}
    tmpl = color_map.get(color, "indigo")

    elements = []
    for line in content_lines:
        if line and line not in ("---", "") and not line.startswith("━"):
            elements.append({"tag": "markdown", "content": line})

    # Footer timestamp
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "UTC: " + now_str}]
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title[:50]},
                "template": tmpl,
            },
            "elements": elements,
        },
    }

    body = json.dumps(card, ensure_ascii=False).encode("utf-8")

    # Try relay proxy first if configured
    if relay_url:
        return _send_via_relay(relay_url, body)

    # Direct webhook
    return _send_direct(webhook_url, body)


def _send_direct(webhook_url: str, body: bytes) -> bool:
    """Send directly to Feishu webhook with retry."""
    max_retries = 2
    last_exc = ""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                webhook_url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.request.URLError as e:
            last_exc = str(e)
            logger.error("Feishu webhook error (attempt %d/%d): %s",
                         attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                time.sleep(1)
        except Exception as e:
            last_exc = str(e)
            logger.error("Feishu send unexpected error: %s", e, exc_info=True)
            return False

    logger.error("Feishu webhook failed after %d attempts: %s",
                 max_retries + 1, last_exc)
    return False


def _send_via_relay(relay_url: str, body: bytes) -> bool:
    """Send via relay proxy (HTTP POST to relay server).
    Relay server forwards the payload to Feishu webhook.
    """
    max_retries = 2
    last_exc = ""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                relay_url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.request.URLError as e:
            last_exc = str(e)
            logger.error("Feishu relay error (attempt %d/%d): %s",
                         attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                time.sleep(1)
        except Exception as e:
            last_exc = str(e)
            logger.error("Feishu relay unexpected error: %s", e, exc_info=True)
            return False

    logger.error("Feishu relay failed after %d attempts: %s",
                 max_retries + 1, last_exc)
    return False
