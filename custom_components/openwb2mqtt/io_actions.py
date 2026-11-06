"""Helpers for openWB IO actions, e.g. Dimm & Control Kit (issue #7).

openWB evaluates the inputs of IO devices (Dimm & Control Kit, EEBus, ...)
via so called *IO actions* ("Dimmen per EMS", "Dimmen per Direktsteuerung",
"RSE-Kontakt", "Stufenweise Steuerung von EZA", ...). Relevant topics:

``<root>/io/action/<id>/config``
    JSON: ``{"name": "Dimmen per EMS", "type": "dimming", "id": 0,
    "group": "controllable_consumers", "configuration": {"io_device": 1,
    "devices": [{"type": "cp", "id": 0}, {"type": "io", "id": 1,
    "digital_output": "DO1"}], ...}}``

``<root>/io/action/<id>/timestamp``
    Unix timestamp (float) since when the action is active, ``null`` if
    the action is not active. Published by openWB when the action is
    (de)activated.

``<root>/chargepoint/<id>/control_parameter/limit``
    JSON: ``{"message": ..., "limiting_value": ...}`` - the reason why the
    charge power of a chargepoint is currently limited (dimming, ripple
    control receiver, load management, ...). ``limiting_value`` is the
    (unformatted) text template of openWB's ``LimitingValue`` enum.

Everything in here is free of Home Assistant imports so it can be unit
tested in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

# Action types that publish ".../timestamp" when they are active. The load
# manager ("load_manager") is continuously active and never publishes it.
IO_ACTION_TYPES_WITH_STATE = {
    "dimming": "mdi:transmission-tower-export",
    "dimming_direct_control": "mdi:flash-alert-outline",
    "ripple_control_receiver": "mdi:signal-variant",
    "stepwise_control": "mdi:solar-power-variant-outline",
}
DEFAULT_IO_ACTION_ICON = "mdi:electric-switch"

IO_ACTION_GROUPS = {
    "controllable_consumers": "Steuerbare Verbrauchseinrichtungen (§14a)",
    "generator_systems": "Erzeugungsanlagen (§9)",
}

# Text templates of openWB's LimitingValue enum (packages/control/
# limiting_value.py) mapped to stable keys. openWB publishes the template
# itself (unformatted) as "limiting_value".
LIMITING_VALUES: dict[str, str] = {
    "current": ", da der Maximal-Strom an Zähler {} erreicht ist.",
    "power": ", da die maximale Leistung an Zähler {} erreicht ist.",
    "unbalanced_load": ", da die maximale Schieflast an Zähler {} erreicht ist.",
    "loadmanager": ", da der Lastmanager die Ladeleistung begrenzt.",
    "loadmanager_error": (
        ", da aufgrund eines Fehlers im Lastmanager die Ladeleistung auf die "
        "maximale Leistung im Fehlerfall begrenzt wird."
    ),
    "dimming": ", da die Dimmung die Ladeleistung begrenzt.",
    "dimming_via_direct_control": (
        ", da die Dimmung per Direkt-Steuerung die Ladeleistung auf 4,2 kW begrenzt."
    ),
    "ripple_control_receiver": (
        ", da der Ladepunkt durch den RSE-Kontakt auf {}% der konfigurierten "
        "Anschlussleistung reduziert wird."
    ),
    "control_stepwise": "Leistung begrenzt auf {}%",
    "controllable_consumers_error": (
        ", da aufgrund eines Fehlers im IO-Gerät {} die steuerbaren Verbraucher "
        "nur mit der minimalen Leistung betrieben werden können. Bitte prüfe die "
        "Status-Seite."
    ),
    "missing_configuration": (
        ", da die Konfiguration für die Aktion unvollständig ist."
    ),
}
LIMIT_NONE = "none"
LIMIT_OTHER = "other"
LIMIT_OPTIONS = [LIMIT_NONE, *LIMITING_VALUES, LIMIT_OTHER]

# Limits that are caused by the grid operator / §14a EnWG control of
# controllable consumers ("Wallbox ist gedimmt").
CONTROLLABLE_CONSUMER_LIMITS = {
    "dimming",
    "dimming_via_direct_control",
    "ripple_control_receiver",
    "controllable_consumers_error",
}

_DEVICE_TYPE_NAMES = {
    "cp": "Ladepunkt",
    "io": "IO-Gerät",
    "inverter": "Wechselrichter",
    "pv": "PV",
    "bat": "Speicher",
    "counter": "Zähler",
}


def _load_json(payload: str | bytes | None) -> Any:
    if payload is None:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    payload = payload.strip()
    if payload == "":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def action_id_from_topic(topic: str) -> str | None:
    """Return the action id from ``.../io/action/<id>/<leaf>``."""
    parts = topic.rstrip("/").split("/")
    try:
        idx = len(parts) - 1 - parts[::-1].index("action")
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    action_id = parts[idx + 1]
    return action_id if action_id.isdigit() else None


def parse_action_config(payload: str | bytes | None) -> dict[str, Any] | None:
    """Parse ``io/action/<id>/config``; ``None`` if removed/invalid."""
    data = _load_json(payload)
    if not isinstance(data, dict) or "type" not in data:
        return None
    return data


def parse_action_timestamp(payload: str | bytes | None) -> datetime | None:
    """Parse ``io/action/<id>/timestamp``; ``None`` means "not active"."""
    data = _load_json(payload)
    if data is None or isinstance(data, bool):
        return None
    try:
        return datetime.fromtimestamp(float(data), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def action_has_state(config: dict[str, Any]) -> bool:
    """Return True if openWB publishes an active/inactive state for it."""
    return config.get("type") in IO_ACTION_TYPES_WITH_STATE


def action_icon(config: dict[str, Any] | None) -> str:
    """Return an icon for the action type."""
    if not config:
        return DEFAULT_IO_ACTION_ICON
    return IO_ACTION_TYPES_WITH_STATE.get(config.get("type"), DEFAULT_IO_ACTION_ICON)


def describe_device(device: dict[str, Any]) -> str:
    """Return a readable name for an entry of ``configuration.devices``."""
    device_type = device.get("type")
    name = _DEVICE_TYPE_NAMES.get(device_type, str(device_type))
    text = f"{name} {device.get('id')}"
    if device.get("digital_output"):
        text += f" ({device['digital_output']})"
    return text


def action_attributes(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the entity attributes for an action config."""
    if not config:
        return {}
    configuration = config.get("configuration") or {}
    devices = configuration.get("devices") or []
    devices = [d for d in devices if isinstance(d, dict)]
    return {
        "action_id": config.get("id"),
        "action_type": config.get("type"),
        "action_group": IO_ACTION_GROUPS.get(config.get("group"), config.get("group")),
        "io_device": configuration.get("io_device"),
        "devices": [describe_device(d) for d in devices],
        "chargepoints": [d.get("id") for d in devices if d.get("type") == "cp"],
    }


def parse_limit(payload: str | bytes | None) -> tuple[str | None, str | None]:
    """Parse ``chargepoint/<id>/control_parameter/limit``.

    Returns ``(key, message)``: key is one of :data:`LIMIT_OPTIONS`
    (``None`` if the payload is unusable), message the readable reason.
    """
    data = _load_json(payload)
    if data is None:
        return None, None
    if not isinstance(data, dict):
        return None, None

    limiting_value = data.get("limiting_value")
    message = data.get("message")
    if isinstance(message, str):
        message = message.strip()
        # openWB messages are sentence continuations (", da die ...").
        message = message.lstrip(", ").strip() or None
        if message:
            message = message[0].upper() + message[1:]
    else:
        message = None

    if limiting_value in (None, ""):
        return LIMIT_NONE, None

    if isinstance(limiting_value, str):
        for key, template in LIMITING_VALUES.items():
            if limiting_value == template or limiting_value == key:
                return key, message
            # Be lenient in case openWB ever publishes the formatted text.
            prefix, _, suffix = template.partition("{}")
            if (
                "{}" in template
                and limiting_value.startswith(prefix)
                and limiting_value.endswith(suffix)
            ):
                return key, message
    return LIMIT_OTHER, message


def limit_is_controllable_consumer(key: str | None) -> bool | None:
    """Return True if the limit is caused by §14a control (dimming/RSE)."""
    if key is None:
        return None
    return key in CONTROLLABLE_CONSUMER_LIMITS


def outputs_used_by_action(config: dict[str, Any] | None, io_device_id) -> set[str]:
    """Return the digital outputs of IO device ``io_device_id`` an action drives.

    - dimming / dimming_direct_control / ripple_control_receiver list them in
      ``configuration.devices`` as ``{"type": "io", "id": 1,
      "digital_output": "DO2"}``.
    - stepwise_control passes the stages through to ``io_output_device``
      via the keys of ``configuration.output_pattern[*].matrix``.

    openWB rewrites these outputs in every control cycle, so a manual
    override would only last a few seconds anyway.
    """
    if not config:
        return set()
    configuration = config.get("configuration") or {}
    io_id = str(io_device_id)
    outputs: set[str] = set()

    for device in configuration.get("devices") or []:
        if (
            isinstance(device, dict)
            and device.get("type") == "io"
            and str(device.get("id")) == io_id
            and device.get("digital_output")
        ):
            outputs.add(str(device["digital_output"]))

    if (
        configuration.get("passthrough_enabled")
        and configuration.get("io_output_device") is not None
        and str(configuration.get("io_output_device")) == io_id
    ):
        for pattern in configuration.get("output_pattern") or []:
            if isinstance(pattern, dict) and isinstance(pattern.get("matrix"), dict):
                outputs.update(str(k) for k in pattern["matrix"])

    return outputs


class IoOutputLockTracker:
    """Tracks which IO actions use which outputs of one IO device."""

    def __init__(self, io_device_id) -> None:
        """Initialize the tracker."""
        self._io_device_id = io_device_id
        self._actions: dict[str, tuple[str, set[str]]] = {}

    def update(self, topic: str, payload) -> bool:
        """Process ``io/action/<id>/config``; return True if anything changed."""
        action_id = action_id_from_topic(topic)
        if action_id is None:
            return False
        config = parse_action_config(payload)
        old = self._actions.get(action_id)
        if config is None:
            self._actions.pop(action_id, None)
            return old is not None
        name = config.get("name") or f"IO-Aktion {action_id}"
        new = (name, outputs_used_by_action(config, self._io_device_id))
        self._actions[action_id] = new
        return old != new

    def locked_by(self, output_name: str) -> list[str]:
        """Return the names of the actions that drive ``output_name``."""
        return sorted(
            name for name, outputs in self._actions.values() if output_name in outputs
        )
