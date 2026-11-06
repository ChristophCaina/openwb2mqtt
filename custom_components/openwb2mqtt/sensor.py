"""The openwbmqtt component for controlling the openWB wallbox via home assistant / MQTT."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util, slugify

from .common import OpenWBBaseEntity, async_setup_sensors
from .const import (
    COMM_METHOD_HTTP,
    COMMUNICATION_METHOD,
    DEVICEID,
    DEVICETYPE,
    DOMAIN,
    MANUFACTURER,
    MQTT_ROOT_TOPIC,
    SENSORS_CONTROLLER,
    SENSORS_PER_BATTERY,
    SENSORS_PER_CHARGEPOINT,
    SENSORS_PER_CHARGEPOINT_CONTROL,
    SENSORS_PER_COUNTER,
    SENSORS_PER_PVGENERATOR,
    SENSORS_PER_VEHICLE,
    openwbDynamicSensorEntityDescription,
    openwbSensorEntityDescription,
)
from .coordinator import OpenWB2MqttDataUpdateCoordinator
from .messages import (
    EVENT_SYSTEM_MESSAGE,
    MAX_MESSAGES_IN_ATTRIBUTES,
    SystemMessage,
    SystemMessageCollection,
    truncate,
)

_LOGGER = logging.getLogger(__name__)

# A message whose timestamp is at most this much older than our subscription
# is treated as "new" even if it was delivered as retained message (e.g. via a
# broker bridge that keeps the retain flag).
SYSTEM_MESSAGE_EVENT_GRACE = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for openWB."""
    device_type = config.data[DEVICETYPE]
    sensors_to_add = []

    if config.data.get(COMMUNICATION_METHOD) == COMM_METHOD_HTTP:
        coordinator = hass.data[DOMAIN][config.entry_id]
        if device_type == "controller":
            for description in SENSORS_CONTROLLER:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiSensor(coordinator, description, config)
                )
        elif device_type == "chargepoint":
            # if device_type == "chargepoint":
            for description in SENSORS_PER_CHARGEPOINT:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiSensor(coordinator, description, config)
                )
        elif device_type == "counter":
            for description in SENSORS_PER_COUNTER:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiSensor(coordinator, description, config)
                )
        elif device_type == "bat":
            for description in SENSORS_PER_BATTERY:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiSensor(coordinator, description, config)
                )
        elif device_type == "pv":
            for description in SENSORS_PER_PVGENERATOR:
                if description.api_key is None:
                    continue
                sensors_to_add.append(
                    OpenWB2MqttApiSensor(coordinator, description, config)
                )
        # Device type vehicle not yet implemented in API
        # elif device_type == "vehicle":
        #     for description in SENSORS_PER_VEHICLE:
        #         if description.api_key is None:
        #             continue
        #         sensors_to_add.append(
        #             OpenWB2MqttApiSensor(coordinator, description, config)
        #         )
        async_add_entities(sensors_to_add)
    else:
        # MQTT setup
        if device_type == "controller":
            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                SENSORS_CONTROLLER,
                "{mqtt_root}/{key}",
                MANUFACTURER,
            )
            # System messages (issue #6): topic ends with a changing id, so
            # these entities subscribe with a wildcard.
            mqtt_root = config.data[MQTT_ROOT_TOPIC]
            device_friendly_name = f"{MANUFACTURER} {config.data[DEVICEID]}"
            async_add_entities(
                [
                    openwbSystemMessagesSensor(
                        uniqueID=config.unique_id,
                        device_friendly_name=device_friendly_name,
                        mqtt_root=mqtt_root,
                    ),
                    openwbLatestSystemMessageSensor(
                        uniqueID=config.unique_id,
                        device_friendly_name=device_friendly_name,
                        mqtt_root=mqtt_root,
                    ),
                ]
            )
        elif device_type == "chargepoint":
            regular_sensors = [
                desc
                for desc in SENSORS_PER_CHARGEPOINT
                if not isinstance(desc, openwbDynamicSensorEntityDescription)
            ] + SENSORS_PER_CHARGEPOINT_CONTROL
            dynamic_sensors = [
                desc
                for desc in SENSORS_PER_CHARGEPOINT
                if isinstance(desc, openwbDynamicSensorEntityDescription)
            ]

            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                regular_sensors,
                "{mqtt_root}/{device_type}/{device_id}/{key}",
                "Chargepoint",
            )

            if dynamic_sensors:
                entities = [
                    openwbDynamicSensor(
                        uniqueID=config.unique_id,
                        description=desc,
                        device_friendly_name=f"Chargepoint {config.data['DEVICEID']}",
                        mqtt_root=config.data["mqttroot"],
                        device_id=config.data["DEVICEID"],
                    )
                    for desc in dynamic_sensors
                ]
                async_add_entities(entities)
        elif device_type == "counter":
            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                SENSORS_PER_COUNTER,
                "{mqtt_root}/{device_type}/{device_id}/get/{key}",
                "Counter",
            )
        elif device_type == "bat":
            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                SENSORS_PER_BATTERY,
                "{mqtt_root}/{device_type}/{device_id}/get/{key}",
                "Battery",
            )
        elif device_type == "pv":
            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                SENSORS_PER_PVGENERATOR,
                "{mqtt_root}/{device_type}/{device_id}/get/{key}",
                "PV",
            )
        elif device_type == "vehicle":

            def process_vehicle_topics(description, device_type, device_id, mqtt_root):
                if description.key == "name":
                    description.mqttTopicCurrentValue = (
                        f"{mqtt_root}/{device_type}/{device_id}/{description.key}"
                    )
                else:
                    description.mqttTopicCurrentValue = (
                        f"{mqtt_root}/{device_type}/{device_id}/get/{description.key}"
                    )

            await async_setup_sensors(
                hass,
                config,
                async_add_entities,
                SENSORS_PER_VEHICLE,
                "{mqtt_root}/{device_type}/{device_id}/get/{key}",
                "Vehicle",
                process_vehicle_topics,
            )


class OpenWB2MqttApiSensor(CoordinatorEntity, SensorEntity):
    """Representation of an openWB sensor that is updated via API."""

    entity_description: openwbSensorEntityDescription

    def __init__(
        self,
        coordinator: OpenWB2MqttDataUpdateCoordinator,
        description: openwbSensorEntityDescription,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.config_entry = config_entry
        self._attr_unique_id = slugify(f"{config_entry.title}_{description.name}")
        self.entity_id = f"sensor.{slugify(f'{config_entry.title}_{description.name}')}"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        key = self.entity_description.api_key
        value = self.coordinator.data.get(key)
        # Transform if value functions and mappings exist
        if self.entity_description.api_value_fn:
            value = self.entity_description.api_value_fn(value)
        elif self.entity_description.value_fn:
            value = self.entity_description.value_fn(value)
        if self.entity_description.valueMap:
            try:
                value = self.entity_description.valueMap.get(int(value))
            except (ValueError, TypeError):
                value = self.entity_description.valueMap.get(value, value)
        return value

    @property
    def icon(self):
        """Return the icon of the sensor."""
        if "phases_in_use" in self.entity_description.key:
            try:
                phases = int(self.native_value)
                match phases:
                    case 0:
                        return "mdi:numeric-0-circle-outline"
                    case 1:
                        return "mdi:numeric-1-circle-outline"
                    case 3:
                        return "mdi:numeric-3-circle-outline"
                    case _:
                        return "mdi:numeric"
            except (ValueError, TypeError):
                pass
        return self.entity_description.icon

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.title)},
            "name": self.config_entry.title,
            "manufacturer": MANUFACTURER,
        }


class openwbSensor(OpenWBBaseEntity, SensorEntity):
    """Representation of an openWB sensor that is updated via MQTT."""

    entity_description: openwbSensorEntityDescription

    def __init__(
        self,
        uniqueID: str | None,
        device_friendly_name: str,
        mqtt_root: str,
        description: openwbSensorEntityDescription,
    ) -> None:
        """Initialize the sensor and the openWB device."""
        super().__init__(
            device_friendly_name=device_friendly_name,
            mqtt_root=mqtt_root,
        )
        self.entity_description = description
        self._attr_unique_id = slugify(f"{uniqueID}_{description.name}")
        self.entity_id = f"sensor.{slugify(f'{uniqueID}_{description.name}')}"
        self._attr_name = description.name

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""

        @callback
        def message_received(message):
            """Handle new MQTT messages."""
            self._attr_native_value = message.payload
            device_registry = dr.async_get(self.hass)
            device = self.device_entry

            if device is None:
                return

            if self.entity_description.value_fn is not None:
                self._attr_native_value = self.entity_description.value_fn(
                    self._attr_native_value
                )
            if self.entity_description.valueMap is not None:
                try:
                    self._attr_native_value = self.entity_description.valueMap.get(
                        int(self._attr_native_value)
                    )
                except (ValueError, TypeError):
                    self._attr_native_value = self.entity_description.valueMap.get(
                        self._attr_native_value, self._attr_native_value
                    )
            if "ip_adress" in self.entity_id:
                device_registry.async_update_device(
                    device.id,
                    configuration_url=f"http://{message.payload.strip('\"')}"
                )
            if "version" in self.entity_id:
                device_registry.async_update_device(
                    device.id, sw_version=message.payload.strip('"')
                )
            if (
                self.entity_description.key == "name"
                and "vehicle" in self.entity_description.mqttTopicCurrentValue
            ):
                device_registry.async_update_device(
                    device.id, name=message.payload.strip('"')
                )
            if "ladepunkt" in self.entity_id:
                try:
                    chargepointInfo = json.loads(message.payload)
                    chargepointName = chargepointInfo.get("name")
                    if chargepointName is not None:
                        device_registry.async_update_device(
                            device.id,
                            name=chargepointName,
                        )
                except json.JSONDecodeError:
                    pass
            if "phases_in_use" in self.entity_description.key:
                try:
                    phases = int(message.payload)
                    if phases == 0:
                        self._attr_icon = "mdi:numeric-0-circle-outline"
                    elif phases == 1:
                        self._attr_icon = "mdi:numeric-1-circle-outline"
                    elif phases == 3:
                        self._attr_icon = "mdi:numeric-3-circle-outline"
                    else:
                        self._attr_icon = "mdi:numeric"
                except (ValueError, TypeError):
                    pass
            self.async_write_ha_state()

        await mqtt.async_subscribe(
            self.hass,
            self.entity_description.mqttTopicCurrentValue,
            message_received,
            1,
        )
        _LOGGER.debug(
            "Subscribed to MQTT topic: %s",
            self.entity_description.mqttTopicCurrentValue,
        )


class openwbDynamicSensor(OpenWBBaseEntity, SensorEntity):
    """Representation of an openWB sensor with dynamic MQTT topic subscription."""

    entity_description: openwbDynamicSensorEntityDescription

    def __init__(
        self,
        uniqueID: str | None,
        device_friendly_name: str,
        mqtt_root: str,
        description: openwbDynamicSensorEntityDescription,
        device_id: int,
    ) -> None:
        """Initialize the sensor and the openWB device."""
        super().__init__(
            device_friendly_name=device_friendly_name,
            mqtt_root=mqtt_root,
        )

        self.entity_description = description
        self._attr_unique_id = slugify(f"{uniqueID}_{description.name}")
        self.entity_id = f"sensor.{slugify(f'{uniqueID}_{description.name}')}"
        self._attr_name = description.name
        self.mqtt_root = mqtt_root
        self.device_id = device_id
        self._charge_template_id = None
        self._unsubscribe_config = None
        self._unsubscribe_template = None

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""
        # First, subscribe to the connected vehicle config topic to get the charge template ID
        config_topic = f"{self.mqtt_root}/chargepoint/{self.device_id}/get/connected_vehicle/config"

        @callback
        def config_message_received(message):
            """Handle new MQTT messages for the config topic."""
            try:
                payload = json.loads(message.payload)
                new_charge_template_id = payload.get("charge_template")

                if (
                    new_charge_template_id is not None
                    and new_charge_template_id != self._charge_template_id
                ):
                    _LOGGER.debug(
                        "Charge template ID changed from %s to %s",
                        self._charge_template_id,
                        new_charge_template_id,
                    )
                    self._charge_template_id = new_charge_template_id

                    # Update the subscription to the charge template topic
                    asyncio.create_task(self._update_template_subscription())
            except (json.JSONDecodeError, ValueError) as err:
                _LOGGER.error("Error parsing config message: %s", err)

        self._unsubscribe_config = await mqtt.async_subscribe(
            self.hass,
            config_topic,
            config_message_received,
            1,
        )
        _LOGGER.debug("Subscribed to config MQTT topic: %s", config_topic)

    async def _update_template_subscription(self):
        """Update the subscription to the charge template topic."""
        # Unsubscribe from the old topic if it exists
        if self._unsubscribe_template is not None:
            self._unsubscribe_template()
            self._unsubscribe_template = None

        if self._charge_template_id is None:
            _LOGGER.debug(
                "No charge template ID available, not subscribing to template topic"
            )
            return

        # Format the template topic with the charge template ID
        template_topic = self.entity_description.mqttTopicTemplate.format(
            mqtt_root=self.mqtt_root,
            charge_template_id=self._charge_template_id,
        )

        @callback
        def template_message_received(message):
            """Handle new MQTT messages for the template topic."""
            try:
                self._attr_native_value = message.payload

                # Convert data if a conversion function is defined
                if self.entity_description.value_fn is not None:
                    self._attr_native_value = self.entity_description.value_fn(
                        self._attr_native_value
                    )

                # Map values as defined in the value map dict if it exists
                if self.entity_description.valueMap is not None:
                    try:
                        self._attr_native_value = self.entity_description.valueMap.get(
                            int(self._attr_native_value)
                        )
                    except ValueError:
                        self._attr_native_value = self.entity_description.valueMap.get(
                            self._attr_native_value, self._attr_native_value
                        )

                # Update entity state with value published on MQTT
                self.async_write_ha_state()
            except Exception as err:
                _LOGGER.error("Error processing template message: %s", err)

        self._unsubscribe_template = await mqtt.async_subscribe(
            self.hass,
            template_topic,
            template_message_received,
            1,
        )
        _LOGGER.debug("Subscribed to template MQTT topic: %s", template_topic)

    async def async_will_remove_from_hass(self):
        """Unsubscribe from MQTT when entity is removed."""
        if self._unsubscribe_config is not None:
            self._unsubscribe_config()

        if self._unsubscribe_template is not None:
            self._unsubscribe_template()


class _openwbSystemMessageBase(OpenWBBaseEntity, SensorEntity):
    """Common base for the system message sensors (issue #6).

    Subscribes to ``<mqtt_root>/system/messages/+`` and keeps all currently
    retained messages in a :class:`SystemMessageCollection`.
    """

    _attr_should_poll = False
    _name: str
    _key: str

    def __init__(
        self,
        uniqueID: str | None,
        device_friendly_name: str,
        mqtt_root: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            device_friendly_name=device_friendly_name,
            mqtt_root=mqtt_root,
        )
        self._attr_name = self._name
        self._attr_unique_id = slugify(f"{uniqueID}_{self._key}")
        self.entity_id = f"sensor.{slugify(f'{uniqueID}_{self._key}')}"
        self._topic = f"{mqtt_root}/system/messages/+"
        self._collection = SystemMessageCollection()
        self._subscribed_at = dt_util.utcnow()
        self._unsubscribe = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT events."""

        @callback
        def message_received(message) -> None:
            new_message = self._collection.update(message.topic, message.payload)
            if new_message is not None and self._is_live(message, new_message):
                self._on_new_message(new_message)
            self._update_from_collection()
            self.async_write_ha_state()

        self._subscribed_at = dt_util.utcnow()
        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, self._topic, message_received, 1
        )
        _LOGGER.debug("Subscribed to MQTT topic: %s", self._topic)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from MQTT."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _is_live(self, message, system_message: SystemMessage) -> bool:
        """Return True if the message was published while we are running.

        Retained messages that are replayed on (re)subscribe are old news and
        must not trigger events again.
        """
        if not getattr(message, "retain", False):
            return True
        if system_message.timestamp is None:
            return False
        return (
            system_message.timestamp
            >= self._subscribed_at - SYSTEM_MESSAGE_EVENT_GRACE
        )

    def _on_new_message(self, system_message: SystemMessage) -> None:
        """Handle a newly published message (default: nothing)."""

    def _update_from_collection(self) -> None:
        """Update the entity attributes from the collection."""
        raise NotImplementedError


class openwbSystemMessagesSensor(_openwbSystemMessageBase):
    """Number of active openWB system messages, details as attributes.

    Additionally fires the ``openwb2mqtt_system_message`` event for every
    message openWB publishes while Home Assistant is running, so it can be
    used as automation trigger (e.g. notification "openWB needs a restart").
    """

    _name = "Systemmeldungen"
    _key = "system_messages"
    _attr_icon = "mdi:message-alert-outline"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {
            "messages": [],
            "highest_severity": None,
        }

    def _on_new_message(self, system_message: SystemMessage) -> None:
        self.hass.bus.async_fire(
            EVENT_SYSTEM_MESSAGE,
            {
                "mqtt_root": self.mqtt_root,
                "device": self.device_friendly_name,
                **system_message.as_dict(),
            },
        )

    def _update_from_collection(self) -> None:
        messages = self._collection.messages
        self._attr_native_value = len(messages)
        self._attr_extra_state_attributes = {
            "messages": [
                m.as_dict() for m in messages[:MAX_MESSAGES_IN_ATTRIBUTES]
            ],
            "highest_severity": self._collection.highest_severity,
        }


class openwbLatestSystemMessageSensor(_openwbSystemMessageBase):
    """Text of the newest openWB system message (HTML stripped)."""

    _name = "Letzte Systemmeldung"
    _key = "latest_system_message"
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the sensor."""
        super().__init__(*args, **kwargs)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _update_from_collection(self) -> None:
        latest = self._collection.latest
        if latest is None:
            self._attr_native_value = None
            self._attr_icon = "mdi:message-text-outline"
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = truncate(latest.message)
        self._attr_icon = _SYSTEM_MESSAGE_ICONS.get(
            latest.type, "mdi:message-text-outline"
        )
        self._attr_extra_state_attributes = latest.as_dict()


_SYSTEM_MESSAGE_ICONS = {
    "info": "mdi:information-outline",
    "success": "mdi:check-circle-outline",
    "warning": "mdi:alert-outline",
    "danger": "mdi:alert-octagon-outline",
}
