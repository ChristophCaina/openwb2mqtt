"""Tests for the IO device via HTTP simpleAPI (openWB >= 2.3.0)."""

from unittest.mock import AsyncMock, MagicMock

from config.custom_components.openwb2mqtt import (
    binary_sensor,
    number,
    sensor,
    switch,
)
from config.custom_components.openwb2mqtt.const import (
    API_URL,
    COMM_METHOD_HTTP,
    COMMUNICATION_METHOD,
    DEVICEID,
    DEVICETYPE,
    DOMAIN,
)
from config.custom_components.openwb2mqtt.coordinator import (
    OpenWB2MqttDataUpdateCoordinator,
    flatten_io_output_response,
)
from homeassistant.core import HomeAssistant
import pytest
from tests.common import MockConfigEntry

# Real answer of openWB 2.3.0 to ?get_io_output_all=auto (Dimm & Control Kit)
REAL_RESPONSE = {
    "io_0": {
        "digital_output": {f"DO{n}": False for n in range(1, 9)},
        "analog_output": [],
    }
}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (REAL_RESPONSE, {f"DO{n}": False for n in range(1, 9)}),
        (
            {"io_2": {"digital_output": {"DO1": True}, "analog_output": {"AO1": 45.5}}},
            {"DO1": True, "AO1": 45.5},
        ),
        ({"io_0": {"digital_output": [], "analog_output": []}}, {}),
        ({"DO1": 0, "AO1": 1.5, "success": True}, {"DO1": 0, "AO1": 1.5}),
        (None, {}),
        ([], {}),
    ],
)
def test_flatten_io_output_response(response, expected) -> None:
    """The nested simpleAPI response is flattened."""
    assert flatten_io_output_response(response) == expected


async def _coordinator(hass: HomeAssistant, entry, response):
    client = MagicMock()
    client.async_get_data = AsyncMock(return_value=response)
    client.async_set_data = AsyncMock()
    coordinator = OpenWB2MqttDataUpdateCoordinator(
        hass, client=client, device_type="io", device_id="auto", config_entry=entry
    )
    coordinator.data = await coordinator._async_update_data()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator, client


async def test_http_io_entities_from_real_response(hass: HomeAssistant) -> None:
    """8 switches + 8 status sensors, nothing else for the real response."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="openWB-io-auto",
        data={
            COMMUNICATION_METHOD: COMM_METHOD_HTTP,
            API_URL: "http://openwb/simpleAPI/simpleapi.php",
            DEVICETYPE: "io",
            DEVICEID: "auto",
        },
    )
    entry.add_to_hass(hass)
    coordinator, client = await _coordinator(hass, entry, REAL_RESPONSE)
    client.async_get_data.assert_called_once_with("?get_io_output_all=auto")

    created = {}
    for name, platform in (
        ("switch", switch),
        ("number", number),
        ("binary_sensor", binary_sensor),
        ("sensor", sensor),
    ):
        entities = []
        await platform.async_setup_entry(hass, entry, entities.extend)
        created[name] = entities

    assert [e.name for e in created["switch"]] == [f"DO{n}" for n in range(1, 9)]
    assert created["number"] == []
    assert [e.name for e in created["binary_sensor"]] == [
        f"DO{n} Status" for n in range(1, 9)
    ]
    assert created["binary_sensor"][0].is_on is False
    assert created["sensor"] == []

    do1 = created["switch"][0]
    assert do1.is_on is False
    await do1.async_turn_on()
    client.async_set_data.assert_called_once_with(
        "set_io_output=1&io_nr=auto&io_output=DO1"
    )
    assert do1.is_on is True
