"""WhatsApp gateway and messaging bridge integration subsystem for Project EDITH v2.0.

This package provides communication with WhatsApp accounts via the companion Node.js bridge:
- `service.py`: Client gateway managing bridge health, pairing, polling incoming messages,
  parallel worker execution with per-chat serialization, and durable outbound delivery retries.
- `session.py`: Fernet encrypted storage for multi-device session credentials.
"""

from .service import WhatsAppGateway
from .session import EncryptedSessionStore

__all__ = ["WhatsAppGateway", "EncryptedSessionStore"]
