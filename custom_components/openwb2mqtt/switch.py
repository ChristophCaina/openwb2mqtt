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
import time

from homeassistant.components import mqtt
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
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
from .io_actions import IoOutputLockTracker

_LOGGER = logging.getLogger(__name__)

# openWB applies manual output changes only in its next control cycle (~10 s)
# and reports the new state afterwards. Until then it keeps reporting the old
# state, which made the switch jump back (HA on -> off -> on). After a command
# the switch therefore shows the requested state until openWB confirms it, at
# most for this many seconds; then the reported state wins again.
PENDING_STATE_TIMEOUT = 30


class PendingSwitchState:
    """Requested switch state that is shown until the device confirms it."""

    def __init__(self, timeout: float = PENDING_STATE_TIMEOUT) -> None:
        """Initialize without a pending state."""
        self.timeout = timeout
        self._target: bool | None = None
        self._deadline = 0.0

    @property
    def active(self) -> bool:
        """Return True while a requested state is waiting for confirmation."""
        return self._target is not None and time.monotonic() < self._deadline

    def set(self, target: bool) -> None:
        """Remember a requested state."""
        self._target = target
        self._deadline = time.monotonic() + self.timeout

    def clear(self) -> None:
        """Forget the requested state."""
        self._target = None

    def resolve(self, actual: bool | None) -> bool | None:
        """Return the state to show for the reported state ``actual``."""
        if not self.active:
            self._target = None
            return actual
        if actual == self._target:
            # Confirmed by openWB.
            self._target = None
            return actual
        return self._target


def _parse_output_value(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on")
    return bool(value)


class _PendingStateSwitch(SwitchEntity):
    """Switch that keeps a requested state until openWB confirms it."""

    _pending: PendingSwitchState
    _cancel_pending_timeout = None

    def _init_pending(self) -> None:
        self._pending = PendingSwitchState()
        self._cancel_pending_timeout = None

    @callback
    def _start_pending(self, target: bool) -> None:
        """Show ``target`` until confirmed or the timeout expires."""
        self._pending.set(target)
        if self._cancel_pending_timeout is not None:
            self._cancel_pending_timeout()

        @callback
        def _expired(_now) -> None:
            self._cancel_pending_timeout = None
            if self._pending.active:
                return
            self._on_pending_expired()
            self.async_write_ha_state()

        # +1 s so the deadline has certainly passed when the callback runs.
        self._cancel_pending_timeout = async_call_later(
            self.hass, self._pending.timeout + 1, _expired
        )

    def _on_pending_expired(self) -> None:
        """Fall back to the last reported state (override if needed)."""

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a running pending timeout."""
        if self._cancel_pending_timeout is not None:
            self._cancel_pending_timeout()
            self._cancel_pending_timeout = None


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


class OpenWB2MqttIOSwitch(CoordinatorEntity, _PendingStateSwitch):
    """A single digital output of an openWB IO device (e.g. Dimm & Control)."""

    def __init__(
        self,
        coordinator: OpenWB2MqttDataUpdateCoordinator,
        output_name: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._init_pending()
        self._output_name = output_name
        self.config_entry = config_entry
        self._attr_name = output_name
        self._attr_unique_id = slugify(f"{config_entry.title}_{output_name}")
        self.entity_id = (
            f"{SWITCH_DOMAIN}.{slugify(f'{config_entry.title}_{output_name}')}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the output is on (requested state while pending)."""
        actual = None
        if self.coordinator.data is not None:
            actual = _parse_output_value(self.coordinator.data.get(self._output_name))
        return self._pending.resolve(actual)

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

        # Show the requested state until openWB confirms it (see
        # PENDING_STATE_TIMEOUT); the coordinator data keeps the real state so
        # the "DOx Status" sensors are not affected.
        self._start_pending(bool(value))
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.title)},
            "name": self.config_entry.title,
            "manufacturer": MANUFACTURER,
        }


class openwbIODigitalOutputSwitch(OpenWBBaseEntity, _PendingStateSwitch):
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
        self._reported_is_on: bool | None = None
        self._init_pending()
        self._state_topic = f"{mqtt_root}/io/states/{device_id}/get/digital_output"
        self._command_topic = (
            f"{mqtt_root}/set/system/io/{device_id}/set/manual/digital_output/"
            f"{output_name}"
        )
        # Outputs driven by an openWB IO action (e.g. "Dimmen per EMS" passing
        # the grid operator's dimming on to a heat pump) are locked: openWB
        # rewrites them every control cycle, and overriding them would bypass
        # the §14a EnWG control.
        self._action_config_topic = f"{mqtt_root}/io/action/+/config"
        self._lock_tracker = IoOutputLockTracker(device_id)
        self._unsubscribers: list = []

    @property
    def locked_by(self) -> list[str]:
        """Names of the openWB IO actions that drive this output."""
        return self._lock_tracker.locked_by(self._output_name)

    @property
    def icon(self) -> str | None:
        """Show a lock while the output is reserved by an IO action."""
        return "mdi:lock" if self.locked_by else None

    @property
    def extra_state_attributes(self) -> dict:
        """Expose whether and by which action the output is locked."""
        return {"locked": bool(self.locked_by), "locked_by": self.locked_by}

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

            self._reported_is_on = _parse_output_value(payload[self._output_name])
            self._attr_is_on = self._pending.resolve(self._reported_is_on)
            self.async_write_ha_state()

        @callback
        def action_config_received(message):
            """Track which IO actions use this output."""
            if self._lock_tracker.update(message.topic, message.payload):
                self.async_write_ha_state()

        self._unsubscribers = [
            await mqtt.async_subscribe(
                self.hass, self._state_topic, message_received, 1
            ),
            await mqtt.async_subscribe(
                self.hass, self._action_config_topic, action_config_received, 1
            ),
        ]
        _LOGGER.debug("Subscribed to IO output MQTT topic: %s", self._state_topic)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from MQTT."""
        await super().async_will_remove_from_hass()
        for unsubscribe in self._unsubscribers:
            if callable(unsubscribe):
                unsubscribe()
        self._unsubscribers = []

    def _on_pending_expired(self) -> None:
        self._attr_is_on = self._reported_is_on

    async def _async_command(self, target: bool) -> None:
        self._raise_if_locked()
        await mqtt.async_publish(
            self.hass, self._command_topic, "true" if target else "false", 0, False
        )
        self._start_pending(target)
        self._attr_is_on = target
        self.async_write_ha_state()

    def _raise_if_locked(self) -> None:
        if self.locked_by:
            raise HomeAssistantError(
                f"{self._output_name} wird von der openWB-Aktion "
                f"'{', '.join(self.locked_by)}' gesteuert und kann nicht manuell "
                "geschaltet werden."
            )

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the output on."""
        await self._async_command(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the output off."""
        await self._async_command(False)
