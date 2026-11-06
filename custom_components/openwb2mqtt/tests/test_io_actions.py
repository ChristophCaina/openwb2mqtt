"""Tests for openWB IO actions / Dimm & Control Kit (issue #7)."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config.custom_components.openwb2mqtt.binary_sensor import (
    async_setup_entry as binary_sensor_setup_entry,
    openwbIoActionBinarySensor,
)
from config.custom_components.openwb2mqtt.const import (
    BINARY_SENSORS_PER_CHARGEPOINT_CONTROL,
    DEVICEID,
    DEVICETYPE,
    DOMAIN,
    MQTT_ROOT_TOPIC,
    SENSORS_PER_CHARGEPOINT_CONTROL,
)
from config.custom_components.openwb2mqtt.io_actions import (
    LIMIT_NONE,
    LIMIT_OPTIONS,
    LIMIT_OTHER,
    LIMITING_VALUES,
    action_attributes,
    action_has_state,
    action_id_from_topic,
    limit_is_controllable_consumer,
    parse_action_config,
    parse_action_timestamp,
    parse_limit,
)
from homeassistant.core import HomeAssistant
import pytest
from tests.common import MockConfigEntry

DIMMING_CONFIG = {
    "name": "Dimmen per EMS",
    "type": "dimming",
    "id": 3,
    "group": "controllable_consumers",
    "configuration": {
        "io_device": 1,
        "input_pattern": [],
        "devices": [
            {"type": "cp", "id": 0},
            {"type": "io", "id": 1, "digital_output": "DO2"},
        ],
        "max_import_power": 0,
    },
}


def _msg(topic, payload, retain=False):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    return SimpleNamespace(topic=topic, payload=payload, retain=retain)


# --- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("openWB/io/action/3/config", "3"),
        ("myroot/openWB/io/action/12/timestamp", "12"),
        ("openWB/io/action/+/config", None),
        ("openWB/io/states/1/get/digital_input", None),
    ],
)
def test_action_id_from_topic(topic, expected) -> None:
    """The action id is extracted from the topic."""
    assert action_id_from_topic(topic) == expected


def test_parse_action_config() -> None:
    """Configs are parsed, removed/invalid payloads give None."""
    assert parse_action_config(json.dumps(DIMMING_CONFIG)) == DIMMING_CONFIG
    assert parse_action_config("") is None
    assert parse_action_config("null") is None
    assert parse_action_config('{"name": "x"}') is None
    assert action_has_state(DIMMING_CONFIG)
    assert not action_has_state({"type": "load_manager"})


@pytest.mark.parametrize("payload", [None, "", "null", "false", "abc"])
def test_parse_action_timestamp_inactive(payload) -> None:
    """Missing timestamp means inactive."""
    assert parse_action_timestamp(payload) is None


def test_parse_action_timestamp_active() -> None:
    """A float timestamp means active since then."""
    assert parse_action_timestamp("1762527574.123") == datetime.fromtimestamp(
        1762527574.123, UTC
    )


def test_action_attributes() -> None:
    """Affected devices are described readable."""
    attrs = action_attributes(DIMMING_CONFIG)
    assert attrs == {
        "action_id": 3,
        "action_type": "dimming",
        "action_group": "Steuerbare Verbrauchseinrichtungen (§14a)",
        "io_device": 1,
        "devices": ["Ladepunkt 0", "IO-Gerät 1 (DO2)"],
        "chargepoints": [0],
    }
    assert action_attributes(None) == {}


@pytest.mark.parametrize(("key", "template"), list(LIMITING_VALUES.items()))
def test_parse_limit_all_known_values(key, template) -> None:
    """Every openWB LimitingValue template maps to its key."""
    message = template.replace("{}", "X")
    assert (
        parse_limit(json.dumps({"message": message, "limiting_value": template}))[0]
        == key
    )
    # formatted variant is accepted too
    assert (
        parse_limit(json.dumps({"message": message, "limiting_value": message}))[0]
        == key
    )


def test_parse_limit() -> None:
    """Message is made readable, special cases handled."""
    key, message = parse_limit(
        json.dumps(
            {
                "message": ", da die Dimmung die Ladeleistung begrenzt.",
                "limiting_value": LIMITING_VALUES["dimming"],
            }
        )
    )
    assert key == "dimming"
    assert message == "Da die Dimmung die Ladeleistung begrenzt."
    assert limit_is_controllable_consumer(key) is True

    assert parse_limit('{"message": null, "limiting_value": null}') == (
        LIMIT_NONE,
        None,
    )
    assert limit_is_controllable_consumer(LIMIT_NONE) is False
    assert parse_limit('{"message": "?", "limiting_value": "neu"}')[0] == (LIMIT_OTHER)
    assert parse_limit("") == (None, None)
    assert limit_is_controllable_consumer(None) is None
    assert limit_is_controllable_consumer("loadmanager") is False
    assert limit_is_controllable_consumer("ripple_control_receiver") is True


def test_chargepoint_descriptions() -> None:
    """The chargepoint descriptions use the parser."""
    payload = json.dumps(
        {
            "message": "x",
            "limiting_value": LIMITING_VALUES["ripple_control_receiver"],
        }
    )
    (sensor,) = SENSORS_PER_CHARGEPOINT_CONTROL
    assert sensor.key == "control_parameter/limit"
    assert sensor.api_key is None
    assert sensor.options == LIMIT_OPTIONS
    assert sensor.value_fn(payload) == "ripple_control_receiver"
    assert sensor.value_fn("") is None

    (binary,) = BINARY_SENSORS_PER_CHARGEPOINT_CONTROL
    assert binary.state(payload) is True
    assert binary.state('{"message": null, "limiting_value": null}') is False


# --- entities -----------------------------------------------------------------


def _entry(device_type: str, device_id=0) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"openwb-{device_type}-{device_id}",
        data={MQTT_ROOT_TOPIC: "openWB", DEVICETYPE: device_type, DEVICEID: device_id},
    )


async def test_controller_discovers_io_actions(hass: HomeAssistant) -> None:
    """One binary sensor per stateful IO action is created once."""
    entry = _entry("controller")
    entry.add_to_hass(hass)
    added = []
    with patch(
        "homeassistant.components.mqtt.async_subscribe",
        new_callable=AsyncMock,
        return_value=MagicMock(),
    ) as mock_subscribe:
        await binary_sensor_setup_entry(
            hass, entry, lambda entities: added.extend(entities)
        )
    mock_subscribe.assert_called_once()
    topic, config_received = mock_subscribe.call_args[0][1:3]
    assert topic == "openWB/io/action/+/config"

    config_received(_msg("openWB/io/action/3/config", DIMMING_CONFIG))
    config_received(_msg("openWB/io/action/3/config", DIMMING_CONFIG))  # dup
    config_received(
        _msg("openWB/io/action/4/config", {"type": "load_manager", "id": 4})
    )
    config_received(_msg("openWB/io/action/5/config", ""))
    config_received(
        _msg(
            "openWB/io/action/6/config",
            {"name": "RSE", "type": "ripple_control_receiver", "id": 6},
        )
    )

    assert [type(e) for e in added] == [openwbIoActionBinarySensor] * 2
    assert [e.entity_id for e in added] == [
        "binary_sensor.openwb_controller_0_io_action_3",
        "binary_sensor.openwb_controller_0_io_action_6",
    ]
    assert added[0].name == "Dimmen per EMS"
    assert added[1].icon == "mdi:signal-variant"


async def test_io_action_binary_sensor(hass: HomeAssistant) -> None:
    """The sensor follows the timestamp topic and config changes."""
    entity = openwbIoActionBinarySensor(
        uniqueID="openwb-controller-0",
        device_friendly_name="openWB 0",
        mqtt_root="openWB",
        action_id="3",
        action_config=DIMMING_CONFIG,
    )
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    unsub = MagicMock()
    with patch(
        "homeassistant.components.mqtt.async_subscribe",
        new_callable=AsyncMock,
        return_value=unsub,
    ) as mock_subscribe:
        await entity.async_added_to_hass()

    callbacks = {c[0][1]: c[0][2] for c in mock_subscribe.call_args_list}
    assert set(callbacks) == {
        "openWB/io/action/3/config",
        "openWB/io/action/3/timestamp",
    }
    on_config = callbacks["openWB/io/action/3/config"]
    on_timestamp = callbacks["openWB/io/action/3/timestamp"]

    assert entity.is_on is False
    assert entity.extra_state_attributes["devices"] == [
        "Ladepunkt 0",
        "IO-Gerät 1 (DO2)",
    ]
    assert entity.extra_state_attributes["active_since"] is None

    on_timestamp(_msg("openWB/io/action/3/timestamp", "1762527574.5"))
    assert entity.is_on is True
    assert entity.extra_state_attributes["active_since"] == (
        datetime.fromtimestamp(1762527574.5, UTC).isoformat()
    )

    on_timestamp(_msg("openWB/io/action/3/timestamp", "null"))
    assert entity.is_on is False

    renamed = {**DIMMING_CONFIG, "name": "Dimmung Netzbetreiber"}
    on_config(_msg("openWB/io/action/3/config", renamed))
    assert entity.name == "Dimmung Netzbetreiber"
    assert entity.available is True

    on_config(_msg("openWB/io/action/3/config", ""))
    assert entity.available is False

    await entity.async_will_remove_from_hass()
    assert unsub.call_count == 2


async def test_chargepoint_setup_adds_limit_entities(hass: HomeAssistant) -> None:
    """MQTT chargepoints get the limit sensor and the dimmed binary sensor."""
    from config.custom_components.openwb2mqtt.sensor import (  # noqa: PLC0415
        async_setup_entry as sensor_setup_entry,
    )

    entry = _entry("chargepoint", 0)
    entry.add_to_hass(hass)

    sensors = []
    await sensor_setup_entry(hass, entry, lambda e: sensors.extend(e))
    limit = [
        s for s in sensors if s.entity_description.key == "control_parameter/limit"
    ]
    assert len(limit) == 1
    assert (
        limit[0].entity_description.mqttTopicCurrentValue
        == "openWB/chargepoint/0/control_parameter/limit"
    )

    binary_sensors = []
    await binary_sensor_setup_entry(hass, entry, lambda e: binary_sensors.extend(e))
    dimmed = [
        b
        for b in binary_sensors
        if b.entity_description.key == "control_parameter/limit"
    ]
    assert len(dimmed) == 1
    assert (
        dimmed[0].entity_description.mqttTopicCurrentValue
        == "openWB/chargepoint/0/control_parameter/limit"
    )
    assert dimmed[0].name == "Gedimmt"
