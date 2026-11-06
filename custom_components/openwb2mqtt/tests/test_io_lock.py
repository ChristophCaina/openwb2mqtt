"""Tests for locking IO output switches used by openWB IO actions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config.custom_components.openwb2mqtt.binary_sensor import (
    openwbIODigitalInputBinarySensor,
)
from config.custom_components.openwb2mqtt.io_actions import (
    IoOutputLockTracker,
    outputs_used_by_action,
)
from config.custom_components.openwb2mqtt.switch import openwbIODigitalOutputSwitch
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

DIMMING = {
    "name": "Dimmen per EMS",
    "type": "dimming",
    "id": 0,
    "configuration": {
        "io_device": 1,
        "devices": [
            {"type": "cp", "id": 0},
            {"type": "io", "id": 1, "digital_output": "DO2"},
            {"type": "io", "id": 5, "digital_output": "DO3"},
        ],
    },
}
STEPWISE = {
    "name": "Stufenweise Steuerung von EZA",
    "type": "stepwise_control",
    "id": 1,
    "configuration": {
        "io_device": 1,
        "passthrough_enabled": True,
        "io_output_device": 1,
        "output_pattern": [
            {"value": 1, "matrix": {"DO5": False, "DO6": False}},
            {"value": 0.6, "matrix": {"DO5": True, "DO6": False}},
        ],
    },
}


def _msg(topic, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return SimpleNamespace(topic=topic, payload=payload, retain=False)


def test_outputs_used_by_action() -> None:
    """Outputs of the given IO device are found for all action kinds."""
    assert outputs_used_by_action(DIMMING, 1) == {"DO2"}
    assert outputs_used_by_action(DIMMING, "5") == {"DO3"}
    assert outputs_used_by_action(STEPWISE, 1) == {"DO5", "DO6"}
    assert outputs_used_by_action(STEPWISE, 2) == set()
    no_passthrough = {
        **STEPWISE,
        "configuration": {**STEPWISE["configuration"], "passthrough_enabled": False},
    }
    assert outputs_used_by_action(no_passthrough, 1) == set()
    assert outputs_used_by_action(None, 1) == set()


def test_lock_tracker() -> None:
    """Actions are added, changed and removed."""
    tracker = IoOutputLockTracker(1)
    assert tracker.update("openWB/io/action/0/config", json.dumps(DIMMING))
    assert not tracker.update("openWB/io/action/0/config", json.dumps(DIMMING))
    assert tracker.update("openWB/io/action/1/config", json.dumps(STEPWISE))
    assert tracker.locked_by("DO2") == ["Dimmen per EMS"]
    assert tracker.locked_by("DO5") == ["Stufenweise Steuerung von EZA"]
    assert tracker.locked_by("DO1") == []
    assert tracker.update("openWB/io/action/0/config", "")
    assert tracker.locked_by("DO2") == []


async def _added(hass, entity):
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    with patch(
        "homeassistant.components.mqtt.async_subscribe",
        new_callable=AsyncMock,
        return_value=MagicMock(),
    ) as mock_subscribe:
        await entity.async_added_to_hass()
    return {c[0][1]: c[0][2] for c in mock_subscribe.call_args_list}


async def test_switch_locked_by_action(hass: HomeAssistant) -> None:
    """A switch whose output is used by an action refuses to switch."""
    switch = openwbIODigitalOutputSwitch(
        uniqueID="openwb-io-1",
        device_friendly_name="IO 1",
        mqtt_root="openWB",
        device_id=1,
        output_name="DO2",
    )
    callbacks = await _added(hass, switch)
    assert set(callbacks) == {
        "openWB/io/states/1/get/digital_output",
        "openWB/io/action/+/config",
    }
    on_action = callbacks["openWB/io/action/+/config"]

    with patch(
        "homeassistant.components.mqtt.async_publish", new_callable=AsyncMock
    ) as mock_publish:
        # free output -> switchable
        assert switch.extra_state_attributes == {"locked": False, "locked_by": []}
        await switch.async_turn_on()
        mock_publish.assert_called_once_with(
            hass,
            "openWB/set/system/io/1/set/manual/digital_output/DO2",
            "true",
            0,
            False,
        )

        # action uses DO2 -> locked
        on_action(_msg("openWB/io/action/0/config", DIMMING))
        assert switch.extra_state_attributes == {
            "locked": True,
            "locked_by": ["Dimmen per EMS"],
        }
        assert switch.icon == "mdi:lock"
        with pytest.raises(HomeAssistantError, match="Dimmen per EMS"):
            await switch.async_turn_off()
        assert mock_publish.call_count == 1

        # action deleted -> free again
        on_action(_msg("openWB/io/action/0/config", ""))
        await switch.async_turn_off()
        assert mock_publish.call_count == 2


async def test_output_status_binary_sensor(hass: HomeAssistant) -> None:
    """Outputs are also available as read-only binary sensors."""
    sensor = openwbIODigitalInputBinarySensor(
        uniqueID="openwb-io-1",
        device_friendly_name="IO 1",
        mqtt_root="openWB",
        device_id=1,
        input_name="DO2",
        io_group="digital_output",
    )
    assert sensor.name == "DO2 Status"
    assert sensor.entity_id == "binary_sensor.openwb_io_1_io_1_do2_status"
    callbacks = await _added(hass, sensor)
    on_state = callbacks["openWB/io/states/1/get/digital_output"]
    on_state(_msg("x", {"DO1": False, "DO2": True}))
    assert sensor.is_on is True

    # inputs keep their old ids
    di = openwbIODigitalInputBinarySensor(
        uniqueID="openwb-io-1",
        device_friendly_name="IO 1",
        mqtt_root="openWB",
        device_id=1,
        input_name="DI1",
    )
    assert di.name == "DI1"
    assert di.entity_id == "binary_sensor.openwb_io_1_io_1_di1"
