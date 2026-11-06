"""Helpers for openWB system messages (issue #6).

openWB publishes system messages (e.g. "please restart the openWB") as
retained JSON payloads on ``<mqtt_root>/system/messages/<ms-timestamp>``:

    {
        "message": "Bitte die openWB <a href=\"...\">neu starten</a>, ...",
        "source": "command",
        "timestamp": 1762527574,
        "type": "success"          # info | success | warning | danger
    }

The last topic level changes for every message, so we subscribe with a
wildcard and keep track of all currently retained messages. An empty
payload removes a message (retained topic was cleared).

Everything in here is free of Home Assistant imports so it can be unit
tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import html
import json
import re
from typing import Any

# Severity order as used by the openWB web UI (lowest -> highest).
MESSAGE_TYPES = ("info", "success", "warning", "danger")
DEFAULT_MESSAGE_TYPE = "info"

# Home Assistant limits the state of an entity to 255 characters.
MAX_STATE_LENGTH = 255
# Number of messages exposed as attribute (keeps the state machine small).
MAX_MESSAGES_IN_ATTRIBUTES = 20

EVENT_SYSTEM_MESSAGE = "openwb2mqtt_system_message"

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")


def strip_html(text: str) -> str:
    """Convert the HTML snippets openWB uses in messages to plain text."""
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def truncate(text: str, max_length: int = MAX_STATE_LENGTH) -> str:
    """Truncate text so it fits into an entity state."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def message_id_from_topic(topic: str) -> str:
    """Return the last topic level, which is the (ms) timestamp id."""
    return topic.rstrip("/").rsplit("/", 1)[-1]


def _to_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class SystemMessage:
    """A single openWB system message."""

    id: str
    message: str
    message_html: str
    type: str
    source: str | None
    timestamp: datetime | None

    @property
    def sort_key(self) -> tuple[float, str]:
        """Sort by timestamp, fall back to the topic id."""
        ts = self.timestamp.timestamp() if self.timestamp else 0.0
        return (ts, self.id)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON serializable representation."""
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "message_html": self.message_html,
            "source": self.source,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


def parse_system_message(
    message_id: str, payload: str | bytes | None
) -> SystemMessage | None:
    """Parse a payload from ``system/messages/<id>``.

    Returns ``None`` if the payload means "message removed" or is unusable.
    """
    if payload is None:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    payload = payload.strip()
    if payload in ("", "null", "{}", '""'):
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Not JSON - treat the raw payload as the message text.
        data = {"message": payload}

    if isinstance(data, str):
        data = {"message": data}
    if not isinstance(data, dict):
        return None

    raw_message = data.get("message")
    if raw_message is None or str(raw_message).strip() == "":
        return None
    raw_message = str(raw_message)

    message_type = str(data.get("type") or DEFAULT_MESSAGE_TYPE).lower()

    timestamp = _to_datetime(data.get("timestamp"))
    if timestamp is None and message_id.isdigit():
        # The topic id is the publishing time in milliseconds.
        timestamp = _to_datetime(int(message_id) / 1000)

    source = data.get("source")
    return SystemMessage(
        id=message_id,
        message=strip_html(raw_message),
        message_html=raw_message,
        type=message_type,
        source=str(source) if source is not None else None,
        timestamp=timestamp,
    )


class SystemMessageCollection:
    """Keeps track of all currently retained openWB system messages."""

    def __init__(self) -> None:
        """Initialize an empty collection."""
        self._messages: dict[str, SystemMessage] = {}

    def __len__(self) -> int:
        """Return the number of active messages."""
        return len(self._messages)

    def update(self, topic: str, payload: str | bytes | None) -> SystemMessage | None:
        """Process an MQTT message.

        Returns the parsed message if it was not known before (new message),
        otherwise ``None`` (update of a known message or removal).
        """
        message_id = message_id_from_topic(topic)
        parsed = parse_system_message(message_id, payload)
        if parsed is None:
            self._messages.pop(message_id, None)
            return None
        is_new = message_id not in self._messages
        self._messages[message_id] = parsed
        return parsed if is_new else None

    @property
    def messages(self) -> list[SystemMessage]:
        """Return all messages, newest first."""
        return sorted(self._messages.values(), key=lambda m: m.sort_key, reverse=True)

    @property
    def latest(self) -> SystemMessage | None:
        """Return the newest message."""
        messages = self.messages
        return messages[0] if messages else None

    @property
    def highest_severity(self) -> str | None:
        """Return the most severe message type (info < success < warning < danger)."""
        if not self._messages:
            return None

        def rank(message_type: str) -> int:
            try:
                return MESSAGE_TYPES.index(message_type)
            except ValueError:
                return 0

        return max((m.type for m in self._messages.values()), key=rank)
