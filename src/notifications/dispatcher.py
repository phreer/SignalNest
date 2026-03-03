"""
dispatcher.py - 通知路由器，调用各启用的通知渠道
"""

import logging

logger = logging.getLogger(__name__)


def dispatch(payload: dict, config: dict, *, require_success: bool = True) -> dict:
    """
    根据 config["notifications"] 中的启用状态，
    将 payload 分发给各通知渠道。

    Returns:
        {
          "enabled_channels": [...],
          "attempted_channels": [...],
          "succeeded_channels": [...],
          "failed_channels": [{"channel": "...", "error": "..."}],
          "success_count": int,
        }

    Raises:
        RuntimeError: require_success=True 且没有任何渠道发送成功时抛出。
    """
    notif_cfg = config.get("notifications", {})
    enabled_channels: list[str] = []
    attempted_channels: list[str] = []
    succeeded_channels: list[str] = []
    failed_channels: list[dict] = []

    if notif_cfg.get("email", {}).get("enabled", False):
        enabled_channels.append("email")
        from src.notifications.email_sender import send_email
        attempted_channels.append("email")
        try:
            if send_email(payload, config):
                succeeded_channels.append("email")
            else:
                logger.error("Email 发送失败")
                failed_channels.append({"channel": "email", "error": "send_email returned False"})
        except Exception as e:
            err = str(e)
            logger.exception(f"Email 发送异常: {err}")
            failed_channels.append({"channel": "email", "error": err})

    if notif_cfg.get("feishu", {}).get("enabled", False):
        enabled_channels.append("feishu")
        from src.notifications.feishu_sender import send_feishu
        attempted_channels.append("feishu")
        try:
            if send_feishu(payload, config):
                succeeded_channels.append("feishu")
            else:
                logger.error("飞书发送失败")
                failed_channels.append({"channel": "feishu", "error": "send_feishu returned False"})
        except Exception as e:
            err = str(e)
            logger.exception(f"飞书发送异常: {err}")
            failed_channels.append({"channel": "feishu", "error": err})

    if notif_cfg.get("wework", {}).get("enabled", False):
        enabled_channels.append("wework")
        from src.notifications.wework_sender import send_wework
        attempted_channels.append("wework")
        try:
            if send_wework(payload, config):
                succeeded_channels.append("wework")
            else:
                logger.error("企业微信发送失败")
                failed_channels.append({"channel": "wework", "error": "send_wework returned False"})
        except Exception as e:
            err = str(e)
            logger.exception(f"企业微信发送异常: {err}")
            failed_channels.append({"channel": "wework", "error": err})

    result = {
        "enabled_channels": enabled_channels,
        "attempted_channels": attempted_channels,
        "succeeded_channels": succeeded_channels,
        "failed_channels": failed_channels,
        "success_count": len(succeeded_channels),
    }

    if not enabled_channels:
        msg = "未启用任何通知渠道，请检查 config.notifications.*.enabled"
        if require_success:
            raise RuntimeError(msg)
        logger.warning(msg)
        return result

    if not succeeded_channels:
        msg = (
            "所有启用的通知渠道均发送失败: "
            + ", ".join(
                f"{item.get('channel')}: {item.get('error', 'unknown error')}"
                for item in failed_channels
            )
        )
        if require_success:
            raise RuntimeError(msg)
        logger.warning(msg)
    else:
        logger.info(f"通知已发送至 {len(succeeded_channels)} 个渠道: {', '.join(succeeded_channels)}")

    return result
