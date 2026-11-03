"""Notification pipeline — Chain of Responsibility."""
from app.notification.delivery import DeliveryChannelRecorder
from app.notification.pipeline import (
    ChannelSelector,
    EmailAdapter,
    InAppAdapter,
    NotificationAdapter,
    PushAdapter,
    SmsAdapter,
)

__all__ = [
    "ChannelSelector", "DeliveryChannelRecorder", "EmailAdapter", "InAppAdapter",
    "NotificationAdapter", "PushAdapter", "SmsAdapter",
]
