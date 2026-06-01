"""
Feishu Bot Notification — sends formatted messages via Feishu webhook.
Used by bot.py for trade alerts, error notifications, and daily reports.
"""
import json
import logging
import time
import requests

logger = logging.getLogger(__name__)


def send_feishu(webhook_url, title, content_lines, color="blue"):
    """
    Send a formatted message to Feishu via incoming webhook.
    
    Args:
        webhook_url: Feishu webhook URL
        title: Message title (becomes a bold header)
        content_lines: List of strings, each becomes a line
        color: "blue", "green", "red", "yellow" for left accent bar
    
    Returns: True if sent successfully, False on failure.
    """
    if not webhook_url:
        logger.warning("send_feishu: no webhook URL configured")
        return False

    # Build post content
    content_parts = []
    content_parts.append({"tag": "markdown", "text": f"**{title}**"})

    if content_lines:
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        separator = {"tag": "hr"}
        content_parts.append(separator)
        for line in content_lines:
            if line == "---":
                content_parts.append(separator)
            elif line == "":
                content_parts.append({"tag": "text", "text": ""})
            elif line.startswith("━━━"):
                # Use section divider
                content_parts.append({"tag": "markdown", "text": f"**{line}**"})
            else:
                content_parts.append({"tag": "markdown", "text": line})
        # Footer timestamp
        content_parts.append({"tag": "markdown", "text": f"_UTC: {now_str}_"})

    # Color mapping
    color_map = {
        "blue": "indigo",
        "green": "green",
        "red": "red",
        "yellow": "yellow",
    }
    feishu_color = color_map.get(color, "indigo")

    payload = {
        "msg_type": "post",
        "content": {
            "zh_cn": {
                "title": title[:50],
                "content": content_parts,
            }
        },
    }

    # Retry up to 2 times on network errors
    max_retries = 2
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            last_exc = "Timeout"
            logger.error(f"Feishu webhook timeout (attempt {attempt+1}/{max_retries+1})")
            if attempt < max_retries:
                time.sleep(1)
        except requests.exceptions.RequestException as e:
            last_exc = str(e)
            logger.error(f"Feishu webhook error (attempt {attempt+1}/{max_retries+1}): {e}")
            if attempt < max_retries:
                time.sleep(1)
        except Exception as e:
            last_exc = str(e)
            logger.error(f"Feishu send unexpected error: {e}", exc_info=True)
            return False

    logger.error(f"Feishu webhook failed after {max_retries+1} attempts: {last_exc}")
    return False
