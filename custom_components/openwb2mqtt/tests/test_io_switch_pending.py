"""The IO switches must not jump back while openWB applies a command (~10 s)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config.custom_components.openwb2mqtt import switch as switch_module
from config.custom_components.openwb2mqtt.switch import (
    PendingSwitchState,
    openwbIODigitalOutputSwitch,
)
from homeassistant.core import HomeAssistant


class FakeClock:
    """Controllable replacement for time.monotonic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pending_switch_state() -> None:
    """Requested state is shown until confirmed or expired."""
    clock = FakeClock()
    with patch.object(switch_module.time, "monotonic", clock):
        pending = PendingSwitchState(timeout=30)
        assert pending.resolve(False) is False

        pending.set(True)
        assert pending.resolve(False) is True  # old state from openWB
        clock.now += 10
        assert pending.resolve(True) is True  # confirmed
        assert not pending.active
        assert pending.resolve(False) is False  # later real change wins

        pending.set(True)
        clock.now += 31  # openWB never applied it
        assert pending.resolve(False) is False


def _state(**outputs):
    return SimpleNamespace(
        topic="openWB/io/states/1/get/digital_output",
        payload=json.dumps(outputs),
        retain=False,
    )


async def test_mqtt_switch_does_not_jump_back(hass: HomeAssistant) -> None:
    """HA on -> openWB still off -> openWB on: the switch stays on."""
    clock = FakeClock()
    entity = openwbIODigitalOutputSwitch(
        uniqueID="openwb-io-1",
        device_friendly_name="IO 1",
        mqtt_root="openWB",
        device_id=1,
        output_name="DO1",
    )
    entity.hass = hass
    written = []
    entity.async_write_ha_state = MagicMock(
        side_effect=lambda: written.append(entity.is_on)
    )
    with (
        patch.object(switch_module.time, "monotonic", clock),
        patch(
            "homeassistant.components.mqtt.async_subscribe",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ) as mock_subscribe,
        patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock),
        patch.object(switch_module, "async_call_later") as mock_call_later,
    ):
        await entity.async_added_to_hass()
        on_state = {c[0][1]: c[0][2] for c in mock_subscribe.call_args_list}[
            "openWB/io/states/1/get/digital_output"
        ]
        on_state(_state(DO1=False))
        assert entity.is_on is False

        await entity.async_turn_on()
        assert entity.is_on is True
        mock_call_later.assert_called_once()

        clock.now += 2
        on_state(_state(DO1=False))  # openWB has not applied it yet
        assert entity.is_on is True

        clock.now += 8
        on_state(_state(DO1=True))  # next control cycle
        assert entity.is_on is True

        on_state(_state(DO1=False))  # real change afterwards
        assert entity.is_on is False

        # Command never applied -> after the timeout the real state is shown
        await entity.async_turn_on()
        expired = mock_call_later.call_args[0][2]
        clock.now += 31
        expired(None)
        assert entity.is_on is False

    # The visible state never flickered: off, on, on, on, off, on, off
    assert written == [False, True, True, True, False, True, False]
