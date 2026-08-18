"""OpenWB Switch Entity - digital IO outputs (e.g. Dimm & Control box).

Two independent implementations:
- HTTP simpleAPI (openWB/core PR #3568, "io geraete auslesen und ansprechen"):
  entities are created dynamically from whatever the API returns. Not usable
  until openWB ships this in a stable release (see coordinator.py).
- MQTT (works TODAY, independent of PR #3568):
  Command : {mqtt_root}/set/system/io/<ID>/set/manual/digital_output/DOn
            (documented on openWB's own IO-device config page)
  State   : {mqtt_root}/io/states/<ID>/get/digital_output
            (confirmed via MQTT Explorer) - a SINGLE JSON message containing
            all outputs at once, e.g. {"DO1": false, "DO2": false, ...}.
  Fixed set of DO1..DOn outputs (IO_MQTT_DIGITAL_OUTPUT_COUNT), since MQTT
  mode has no equivalent of "ask the API what outputs exist".
"""

from __future__ import annotations

import json
import logging

from homeassistant.components import mqtt
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .common import OpenWBBaseEntity
from .const import (
    COMM_METHOD_HTTP,
    COMMUNICATION_METHOD,
    DEVICEID,
    DEVICETYPE,
    DOMAIN,
    IO_MQTT_DIGITAL_OUTPUT_COUNT,
    IO_PREFIX_DIGITAL_OUTPUT,
    MANUFACTURER,
    MQTT_ROOT_TOPIC,
)
from .coordinator import OpenWB2MqttDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up switch entities for openWB IO devices."""
    if config.data.get(DEVICETYPE) != "io":
        return

    if config.data.get(COMMUNICATION_METHOD) == COMM_METHOD_HTTP:
        coordinator: OpenWB2MqttDataUpdateCoordinator = hass.data[DOMAIN][
            config.entry_id
        ]
        if not coordinator.data:
            _LOGGER.debug(
                "No IO outputs found yet for %s, no switch entities created",
                config.title,
            )
            return

        entities = [
            OpenWB2MqttIOSwitch(coordinator, output_name, config)
            for output_name in coordinator.data
            if output_name.lower().startswith(IO_PREFIX_DIGITAL_OUTPUT)
        ]
        async_add_entities(entities)
        return

    # MQTT mode: openWB's manual digital-output override, works independently
    # of the (currently unmerged-into-stable) simpleAPI "total"/"io" features.
    mqtt_root = config.data[MQTT_ROOT_TOPIC]
    device_id = config.data[DEVICEID]
    entities = [
        openwbIODigitalOutputSwitch(
            uniqueID=config.unique_id,
            device_friendly_name=f"IO {device_id}",
            mqtt_root=mqtt_root,
            device_id=device_id,
            output_name=f"DO{n}",
        )
        for n in range(1, IO_MQTT_DIGITAL_OUTPUT_COUNT + 1)
    ]
    async_add_entities(entities)


class OpenWB2MqttIOSwitch(CoordinatorEntity, SwitchEntity):
    """A single digital output of an openWB IO device (e.g. Dimm & Control)."""

    def __init__(
        self,
        coordinator: OpenWB2MqttDataUpdateCoordinator,
        output_name: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._output_name = output_name
        self.config_entry = config_entry
        self._attr_name = output_name
        self._attr_unique_id = slugify(f"{config_entry.title}_{output_name}")
        self.entity_id = (
            f"{SWITCH_DOMAIN}.{slugify(f'{config_entry.title}_{output_name}')}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the output is on."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._output_name)
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on")
        return bool(value)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the output on."""
        await self._async_set_output(1)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the output off."""
        await self._async_set_output(0)

    async def _async_set_output(self, value: int) -> None:
        """Write the output value via the openWB simpleAPI.

        Uses ?set_io_output=<value>&io_nr=<id>&io_output=<name> (POST), see
        openWB/core PR #3568.
        """
        io_id = self.coordinator.device_id
        payload = f"set_io_output={value}&io_nr={io_id}&io_output={self._output_name}"
        try:
            await self.coordinator.client.async_set_data(payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Error setting IO output %s (id=%s) to %s",
                self._output_name,
                io_id,
                value,
            )
            return

        # Optimistic update; the next coordinator refresh (15s) will correct it
        # if openWB rejected the change.
        if self.coordinator.data is not None:
            new_data = dict(self.coordinator.data)
            new_data[self._output_name] = value
            self.coordinator.async_set_updated_data(new_data)

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.title)},
            "name": self.config_entry.title,
            "manufacturer": MANUFACTURER,
        }


class openwbIODigitalOutputSwitch(OpenWBBaseEntity, SwitchEntity):
    """A single digital output of an openWB IO device, controlled via MQTT.

    - Command: {mqtt_root}/set/system/io/{id}/set/manual/digital_output/{name}
      (documented on openWB's own IO-device config page; per-output topic).
    - State: {mqtt_root}/io/states/{id}/get/digital_output
      (confirmed via MQTT Explorer; ONE shared topic/JSON message for all
      outputs, e.g. {"DO1": false, "DO2": false, ...} - each entity
      subscribes to the same topic and picks out its own key).
    """

    def __init__(
        self,
        uniqueID: str,
        device_friendly_name: str,
        mqtt_root: str,
        device_id: int,
        output_name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(device_friendly_name=device_friendly_name, mqtt_root=mqtt_root)
        self._output_name = output_name
        self._attr_name = output_name
        self._attr_unique_id = slugify(f"{uniqueID}-io-{device_id}-{output_name}")
        self.entity_id = (
            f"{SWITCH_DOMAIN}.{slugify(f'{uniqueID}_io_{device_id}_{output_name}')}"
        )
        self._attr_is_on = None
        self._state_topic = f"{mqtt_root}/io/states/{device_id}/get/digital_output"
        self._command_topic = (
            f"{mqtt_root}/set/system/io/{device_id}/set/manual/digital_output/"
            f"{output_name}"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT events."""

        @callback
        def message_received(message):
            """Handle new MQTT messages (state update).

            Payload is a JSON object with ALL outputs, e.g.
            {"DO1": false, "DO2": false, ...}. Pick out our own key.
            """
            try:
                payload = json.loads(message.payload)
            except (json.JSONDecodeError, TypeError):
                _LOGGER.warning(
                    "Could not parse JSON on topic %s: %s",
                    self._state_topic,
                    message.payload,
                )
                return

            if self._output_name not in payload:
                return

            value = payload[self._output_name]
            if isinstance(value, str):
                self._attr_is_on = value.strip().lower() in ("true", "1", "on")
            else:
                self._attr_is_on = bool(value)
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic, message_received, 1)
        _LOGGER.debug("Subscribed to IO output MQTT topic: %s", self._state_topic)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the output on."""
        await mqtt.async_publish(self.hass, self._command_topic, "true", 0, False)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the output off."""
        await mqtt.async_publish(self.hass, self._command_topic, "false", 0, False)
