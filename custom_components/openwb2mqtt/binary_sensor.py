"""The openwbmqtt component for controlling the openWB wallbox via home assistant / MQTT."""

from __future__ import annotations

import json
import logging

from homeassistant.components import mqtt
from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .common import OpenWBBaseEntity, async_setup_binary_sensors
from .const import (
    BINARY_SENSORS_PER_BATTERY,
    BINARY_SENSORS_PER_CHARGEPOINT,
    BINARY_SENSORS_PER_COUNTER,
    BINARY_SENSORS_PER_PVGENERATOR,
    BINARY_SENSORS_PER_VEHICLE,
    COMM_METHOD_HTTP,
    COMMUNICATION_METHOD,
    DEVICEID,
    DEVICETYPE,
    DOMAIN,
    IO_MQTT_DIGITAL_INPUT_COUNT,
    IO_PREFIX_DIGITAL_INPUT,
    MANUFACTURER,
    MQTT_ROOT_TOPIC,
    openwbBinarySensorEntityDescription,
)
from .coordinator import OpenWB2MqttDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors for openWB."""
    device_type = config.data[DEVICETYPE]
    sensors_to_add = []

    if config.data.get(COMMUNICATION_METHOD) == COMM_METHOD_HTTP:
        coordinator = hass.data[DOMAIN][config.entry_id]
        if device_type == "chargepoint":
            for description in BINARY_SENSORS_PER_CHARGEPOINT:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiBinarySensor(coordinator, description, config)
                )
        elif device_type == "counter":
            for description in BINARY_SENSORS_PER_COUNTER:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiBinarySensor(coordinator, description, config)
                )
        elif device_type == "bat":
            for description in BINARY_SENSORS_PER_BATTERY:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiBinarySensor(coordinator, description, config)
                )
        elif device_type == "pv":
            for description in BINARY_SENSORS_PER_PVGENERATOR:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiBinarySensor(coordinator, description, config)
                )
        elif device_type == "io":
            # Dynamic: one binary_sensor per digital input found in the API
            # response (e.g. "DI1"), see openWB/core PR #3568. Read-only.
            if not coordinator.data:
                _LOGGER.debug(
                    "No IO inputs found yet for %s, no binary_sensor entities "
                    "created",
                    config.title,
                )
            else:
                sensors_to_add.extend(
                    OpenWB2MqttIOBinarySensor(coordinator, input_name, config)
                    for input_name in coordinator.data
                    if input_name.lower().startswith(IO_PREFIX_DIGITAL_INPUT)
                )
        # Device type vehicle not yet implemented in API
        # elif device_type == "vehicle":
        #     for description in BINARY_SENSORS_PER_VEHICLE:
        #         if description.api_key is None:
        #             continue
        #         sensors_to_add.append(
        #             OpenWB2MqttApiBinarySensor(coordinator, description, config)
        #         )
        async_add_entities(sensors_to_add)
    else:
        topic_template = "{mqtt_root}/{device_type}/{device_id}/get/{key}"
        if device_type == "chargepoint":
            await async_setup_binary_sensors(
                hass,
                config,
                async_add_entities,
                BINARY_SENSORS_PER_CHARGEPOINT,
                topic_template,
                "Chargepoint",
            )
        elif device_type == "counter":
            await async_setup_binary_sensors(
                hass,
                config,
                async_add_entities,
                BINARY_SENSORS_PER_COUNTER,
                topic_template,
                "Counter",
            )
        elif device_type == "bat":
            await async_setup_binary_sensors(
                hass,
                config,
                async_add_entities,
                BINARY_SENSORS_PER_BATTERY,
                topic_template,
                "Battery",
            )
        elif device_type == "pv":
            await async_setup_binary_sensors(
                hass,
                config,
                async_add_entities,
                BINARY_SENSORS_PER_PVGENERATOR,
                topic_template,
                "PV",
            )
        elif device_type == "vehicle":
            await async_setup_binary_sensors(
                hass,
                config,
                async_add_entities,
                BINARY_SENSORS_PER_VEHICLE,
                topic_template,
                "Vehicle",
            )
        elif device_type == "io":
            # Digital inputs (DI1..DIn), read-only. State topic confirmed via
            # MQTT Explorer: ONE JSON message with all inputs, e.g.
            # {"DI1": false, "DI2": false, ...}.
            mqtt_root = config.data[MQTT_ROOT_TOPIC]
            device_id = config.data[DEVICEID]
            entities = [
                openwbIODigitalInputBinarySensor(
                    uniqueID=config.unique_id,
                    device_friendly_name=f"IO {device_id}",
                    mqtt_root=mqtt_root,
                    device_id=device_id,
                    input_name=f"DI{n}",
                )
                for n in range(1, IO_MQTT_DIGITAL_INPUT_COUNT + 1)
            ]
            async_add_entities(entities)


class OpenWB2MqttApiBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of an openWB binary sensor that is updated via API."""

    entity_description: openwbBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: OpenWB2MqttDataUpdateCoordinator,
        description: openwbBinarySensorEntityDescription,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.config_entry = config_entry
        self._attr_unique_id = slugify(f"{config_entry.title}_{description.name}")
        self.entity_id = f"{BINARY_SENSOR_DOMAIN}.{slugify(f'{config_entry.title}_{description.name}')}"

    @property
    def is_on(self):
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        key = self.entity_description.api_key or self.entity_description.key
        value = self.coordinator.data.get(key)

        if self.entity_description.state:
            return self.entity_description.state(value)

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"true", "on"}:
                return True
            if value in {"false", "off"}:
                return False

        try:
            return bool(int(value))
        except (ValueError, TypeError):
            return None

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.title)},
            "name": self.config_entry.title,
            "manufacturer": MANUFACTURER,
        }


class OpenWB2MqttIOBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """A single digital input of an openWB IO device (e.g. Dimm & Control).

    Read-only: openWB's simpleAPI only exposes set_io_output for outputs, not
    inputs (openWB/core PR #3568). Entities are created dynamically - one per
    digital input key (e.g. "DI1") found in the coordinator response.
    """

    def __init__(
        self,
        coordinator: OpenWB2MqttDataUpdateCoordinator,
        input_name: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._input_name = input_name
        self.config_entry = config_entry
        self._attr_name = input_name
        self._attr_unique_id = slugify(f"{config_entry.title}_{input_name}")
        self.entity_id = (
            f"{BINARY_SENSOR_DOMAIN}.{slugify(f'{config_entry.title}_{input_name}')}"
        )

    @property
    def is_on(self):
        """Return the state of the digital input."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._input_name)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"true", "on"}:
                return True
            if value in {"false", "off"}:
                return False
        try:
            return bool(int(value))
        except (ValueError, TypeError):
            return None

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.title)},
            "name": self.config_entry.title,
            "manufacturer": MANUFACTURER,
        }


class openwbIODigitalInputBinarySensor(OpenWBBaseEntity, BinarySensorEntity):
    """A single digital input of an openWB IO device, read via MQTT.

    Read-only. State topic confirmed via MQTT Explorer:
    {mqtt_root}/io/states/{id}/get/digital_input - ONE shared JSON message
    for all inputs, e.g. {"DI1": false, "DI2": false, ...}.
    """

    def __init__(
        self,
        uniqueID: str,
        device_friendly_name: str,
        mqtt_root: str,
        device_id: int,
        input_name: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(device_friendly_name=device_friendly_name, mqtt_root=mqtt_root)
        self._input_name = input_name
        self._attr_name = input_name
        self._attr_unique_id = slugify(f"{uniqueID}-io-{device_id}-{input_name}")
        self.entity_id = (
            f"{BINARY_SENSOR_DOMAIN}."
            f"{slugify(f'{uniqueID}_io_{device_id}_{input_name}')}"
        )
        self._attr_is_on = None
        self._state_topic = f"{mqtt_root}/io/states/{device_id}/get/digital_input"

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT events."""

        @callback
        def message_received(message):
            """Handle new MQTT messages (state update)."""
            try:
                payload = json.loads(message.payload)
            except (json.JSONDecodeError, TypeError):
                _LOGGER.warning(
                    "Could not parse JSON on topic %s: %s",
                    self._state_topic,
                    message.payload,
                )
                return

            if self._input_name not in payload:
                return

            value = payload[self._input_name]
            if isinstance(value, str):
                self._attr_is_on = value.strip().lower() in ("true", "1", "on")
            else:
                self._attr_is_on = bool(value)
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic, message_received, 1)
        _LOGGER.debug("Subscribed to IO input MQTT topic: %s", self._state_topic)


class openwbBinarySensor(OpenWBBaseEntity, BinarySensorEntity):
    """Representation of an openWB sensor that is updated via MQTT."""

    entity_description: openwbBinarySensorEntityDescription

    def __init__(
        self,
        uniqueID: str | None,
        device_friendly_name: str,
        mqtt_root: str,
        description: openwbBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor and the openWB device."""
        super().__init__(
            device_friendly_name=device_friendly_name,
            mqtt_root=mqtt_root,
        )

        self.entity_description = description
        self._attr_unique_id = slugify(f"{uniqueID}-{description.name}")
        self.entity_id = (
            f"{BINARY_SENSOR_DOMAIN}.{slugify(f'{uniqueID}_{description.name}')}"
        )
        self._attr_name = description.name

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""

        @callback
        def message_received(message):
            """Handle new MQTT messages."""
            payload = message.payload

            # Use custom state function if defined
            if (
                hasattr(self.entity_description, "state")
                and self.entity_description.state is not None
            ):
                self._attr_is_on = self.entity_description.state(payload)
            else:
                # Default behavior
                payload = payload.strip().lower()
                try:
                    self._attr_is_on = bool(int(payload))
                except ValueError:
                    if payload in {"true", "on"}:
                        self._attr_is_on = True
                    elif payload in {"false", "off"}:
                        self._attr_is_on = False
                    else:
                        self._attr_is_on = None

            # Update entity state with value published on MQTT.
            self.async_write_ha_state()

        await mqtt.async_subscribe(
            self.hass,
            self.entity_description.mqttTopicCurrentValue,
            message_received,
            1,
        )
