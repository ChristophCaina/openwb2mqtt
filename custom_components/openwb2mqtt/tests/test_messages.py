"""Tests for openWB system messages (issue #6)."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config.custom_components.openwb2mqtt.messages import (
    EVENT_SYSTEM_MESSAGE,
    MAX_STATE_LENGTH,
    SystemMessageCollection,
    message_id_from_topic,
    parse_system_message,
    strip_html,
    truncate,
)
from config.custom_components.openwb2mqtt.sensor import (
    openwbLatestSystemMessageSensor,
    openwbSystemMessagesSensor,
)
from homeassistant.core import HomeAssistant
import pytest

RESTART_MESSAGE = {
    "message": 'Bitte die openWB <a href="/openWB/web/settings/#/System/SystemConfiguration">neu starten</a>, damit die Änderungen übernommen werden.',
    "source": "command",
    "timestamp": 1762527574,
    "type": "success",
}
TOPIC = "openWB/system/messages/1762527574575"


def _msg(topic, payload, retain=False):
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return SimpleNamespace(topic=topic, payload=payload, retain=retain)


# --- pure helpers -------------------------------------------------------------


def test_strip_html() -> None:
    """HTML tags/entities are removed, line breaks kept."""
    assert (
        strip_html('Bitte <a href="x">neu starten</a> &amp; prüfen.<br/>Danke')
        == "Bitte neu starten & prüfen.\nDanke"
    )


def test_truncate() -> None:
    """Long texts are cut to the HA state limit."""
    assert truncate("kurz") == "kurz"
    long_text = "x" * 400
    result = truncate(long_text)
    assert len(result) == MAX_STATE_LENGTH
    assert result.endswith("…")


def test_message_id_from_topic() -> None:
    """The last topic level is the id."""
    assert message_id_from_topic(TOPIC) == "1762527574575"


def test_parse_system_message() -> None:
    """The documented example payload is parsed."""
    parsed = parse_system_message("1762527574575", json.dumps(RESTART_MESSAGE))
    assert parsed is not None
    assert parsed.message == (
        "Bitte die openWB neu starten, damit die Änderungen übernommen werden."
    )
    assert parsed.message_html == RESTART_MESSAGE["message"]
    assert parsed.type == "success"
    assert parsed.source == "command"
    assert parsed.timestamp == datetime.fromtimestamp(1762527574, UTC)


@pytest.mark.parametrize("payload", [None, "", "null", "{}", '""', b"", "[]"])
def test_parse_system_message_removed(payload) -> None:
    """Empty payloads mean the message was removed."""
    assert parse_system_message("1", payload) is None


def test_parse_system_message_fallbacks() -> None:
    """Missing type/timestamp and non-JSON payloads are handled."""
    parsed = parse_system_message("1762527574575", '{"message": "Hallo"}')
    assert parsed.type == "info"
    # timestamp derived from the ms id in the topic
    assert parsed.timestamp == datetime.fromtimestamp(1762527574.575, UTC)

    parsed = parse_system_message("abc", "kein json")
    assert parsed.message == "kein json"
    assert parsed.timestamp is None


def test_collection() -> None:
    """New/updated/removed messages are tracked and sorted newest first."""
    collection = SystemMessageCollection()
    first = collection.update(
        "openWB/system/messages/1000",
        json.dumps({"message": "alt", "timestamp": 1, "type": "warning"}),
    )
    assert first is not None
    second = collection.update(
        "openWB/system/messages/2000", json.dumps({"message": "neu", "timestamp": 2})
    )
    assert second is not None
    # update of an already known message is not "new"
    assert (
        collection.update(
            "openWB/system/messages/2000",
            json.dumps({"message": "neu2", "timestamp": 2}),
        )
        is None
    )
    assert len(collection) == 2
    assert [m.message for m in collection.messages] == ["neu2", "alt"]
    assert collection.latest.message == "neu2"
    assert collection.highest_severity == "warning"

    # removal
    assert collection.update("openWB/system/messages/2000", "") is None
    assert len(collection) == 1
    assert collection.latest.message == "alt"
    collection.update("openWB/system/messages/1000", "")
    assert collection.latest is None
    assert collection.highest_severity is None


# --- entities -----------------------------------------------------------------


async def _setup_entity(hass: HomeAssistant, entity_cls):
    entity = entity_cls(
        uniqueID="openwb-controller",
        device_friendly_name="openWB 0",
        mqtt_root="openWB",
    )
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    with patch(
        "homeassistant.components.mqtt.async_subscribe", new_callable=AsyncMock
    ) as mock_subscribe:
        await entity.async_added_to_hass()
    mock_subscribe.assert_called_once()
    args = mock_subscribe.call_args[0]
    assert args[1] == "openWB/system/messages/+"
    return entity, args[2]


async def test_system_messages_sensor(hass: HomeAssistant) -> None:
    """Count + attributes are updated, event only for live messages."""
    entity, message_received = await _setup_entity(hass, openwbSystemMessagesSensor)
    assert entity.entity_id == "sensor.openwb_controller_system_messages"
    assert entity.name == "Systemmeldungen"

    events = []
    hass.bus.async_listen(EVENT_SYSTEM_MESSAGE, events.append)

    # Old retained message replayed on subscribe -> no event
    message_received(_msg(TOPIC, RESTART_MESSAGE, retain=True))
    await hass.async_block_till_done()
    assert entity.native_value == 1
    assert entity.extra_state_attributes["highest_severity"] == "success"
    assert entity.extra_state_attributes["messages"][0]["message"].startswith(
        "Bitte die openWB neu starten"
    )
    assert events == []

    # New message published while running -> event
    now = int(datetime.now(UTC).timestamp())
    live = {"message": "Fehler <b>X</b>", "timestamp": now, "type": "danger"}
    message_received(_msg(f"openWB/system/messages/{now * 1000}", live))
    await hass.async_block_till_done()
    assert entity.native_value == 2
    assert entity.extra_state_attributes["highest_severity"] == "danger"
    assert len(events) == 1
    assert events[0].data["message"] == "Fehler X"
    assert events[0].data["type"] == "danger"
    assert events[0].data["mqtt_root"] == "openWB"

    # Same message again (e.g. re-publish) -> no second event
    message_received(_msg(f"openWB/system/messages/{now * 1000}", live))
    await hass.async_block_till_done()
    assert len(events) == 1

    # Retained but fresh (broker bridge keeps retain flag) -> event
    fresh = {"message": "frisch", "timestamp": now, "type": "info"}
    message_received(_msg(f"openWB/system/messages/{now * 1000 + 1}", fresh, True))
    await hass.async_block_till_done()
    assert len(events) == 2

    # Removing messages
    message_received(_msg(TOPIC, ""))
    assert entity.native_value == 2


async def test_latest_system_message_sensor(hass: HomeAssistant) -> None:
    """The newest message text is the state, details are attributes."""
    entity, message_received = await _setup_entity(
        hass, openwbLatestSystemMessageSensor
    )
    assert entity.entity_id == "sensor.openwb_controller_latest_system_message"
    assert entity.native_value is None

    message_received(_msg(TOPIC, RESTART_MESSAGE, retain=True))
    assert entity.native_value == (
        "Bitte die openWB neu starten, damit die Änderungen übernommen werden."
    )
    assert entity.extra_state_attributes["type"] == "success"
    assert entity.extra_state_attributes["message_html"] == RESTART_MESSAGE["message"]
    assert entity.icon == "mdi:check-circle-outline"

    old = datetime.fromtimestamp(1762527574, UTC) - timedelta(days=1)
    message_received(
        _msg(
            "openWB/system/messages/1",
            {"message": "älter", "timestamp": int(old.timestamp()), "type": "warning"},
        )
    )
    # still the newest one
    assert entity.native_value.startswith("Bitte die openWB")

    message_received(_msg(TOPIC, ""))
    assert entity.native_value == "älter"
    assert entity.icon == "mdi:alert-outline"

    message_received(_msg("openWB/system/messages/1", ""))
    assert entity.native_value is None
    assert entity.extra_state_attributes == {}


async def test_controller_setup_adds_message_sensors(hass: HomeAssistant) -> None:
    """MQTT controller entries get both system message sensors."""
    from config.custom_components.openwb2mqtt.const import (  # noqa: PLC0415
        DEVICEID,
        DEVICETYPE,
        DOMAIN,
        MQTT_ROOT_TOPIC,
    )
    from config.custom_components.openwb2mqtt.sensor import (  # noqa: PLC0415
        async_setup_entry,
    )
    from tests.common import MockConfigEntry  # noqa: PLC0415

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="openwb-controller",
        data={MQTT_ROOT_TOPIC: "openWB", DEVICETYPE: "controller", DEVICEID: 0},
    )
    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    types = {type(e) for e in added}
    assert openwbSystemMessagesSensor in types
    assert openwbLatestSystemMessageSensor in types
