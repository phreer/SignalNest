"""
dispatcher.py - 通知路由器，调用各启用的通知渠道
"""

import logging

logger = logging.getLogger(__name__)


def dispatch(payload: dict, config: dict) -> None:
    """
    根据 config["notifications"] 中的启用状态，
    将 payload 分发给各通知渠道。
    """
    notif_cfg = config.get("notifications", {})
    sent = 0

    if notif_cfg.get("email", {}).get("enabled", False):
        from src.notifications.email_sender import send_email
        if send_email(payload, config):
            sent += 1
        else:
            logger.error("Email 发送失败")

    if notif_cfg.get("feishu", {}).get("enabled", False):
        from src.notifications.feishu_sender import send_feishu
        if send_feishu(payload, config):
            sent += 1
        else:
            logger.error("飞书发送失败")

    if notif_cfg.get("wework", {}).get("enabled", False):
        from src.notifications.wework_sender import send_wework
        if send_wework(payload, config):
            sent += 1
        else:
            logger.error("企业微信发送失败")

    if sent == 0:
        logger.warning("所有通知渠道均未发送成功，请检查配置")
    else:
        logger.info(f"通知已发送至 {sent} 个渠道")
