"""Data update coordinator for the openWB2 MQTT integration."""

from __future__ import annotations

from datetime import timedelta
import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify

from .api_client import (
    OpenWB2MqttApiClient,
    OpenWB2MqttApiClientCommunicationError,
    OpenWB2MqttApiClientError,
)
from .const import CONF_VEHICLES, DOMAIN, TOTAL_ID

_LOGGER = logging.getLogger(__name__)

# Metrics that openWB's simpleAPI can aggregate across all component IDs when
# queried with the pseudo-ID "total" (openWB/core PR #3568). Both pv and
# chargepoint support the same set of metrics.
TOTAL_METRICS = ("power", "imported", "exported", "monthly_exported", "yearly_exported")


class OpenWB2MqttDataUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the openWB2 MQTT integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OpenWB2MqttApiClient,
        device_type: str,
        device_id: int | str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the data update coordinator."""
        self._device_type = device_type
        self._device_id = device_id
        self.config_entry = config_entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device_type}_{device_id}",
            update_interval=timedelta(seconds=15),
            update_method=self._async_update_data,
        )
        self.config_entry = config_entry
        self.client = client
        self._name_to_id_map = None
        # "total" pseudo-ID: pv/chargepoint aggregated across all IDs (openWB/core
        # PR #3568), entered by the user in place of a numeric device ID.
        self._is_total = (
            device_type in ("pv", "chargepoint")
            and str(device_id).lower() == TOTAL_ID
        )

    @property
    def device_id(self) -> int | str:
        """Return the configured device/IO ID (may be numeric or a pseudo-ID)."""
        return self._device_id

    @property
    def vehicle_name_to_id_map(self) -> dict[str, str]:
        """Return a mapping of vehicle names to IDs."""
        if self._name_to_id_map is None:
            vehicles = self.config_entry.options.get(
                CONF_VEHICLES, self.config_entry.data.get(CONF_VEHICLES, {})
            )
            self._name_to_id_map = {v: k for k, v in vehicles.items()}
        return self._name_to_id_map

    async def _async_update_data(self):
        """Update data via library."""
        try:
            if self._is_total:
                # "total": aggregated across all IDs of this component (pv/chargepoint)
                return await self._async_get_total(self._device_type)
            if self._device_type == "chargepoint":
                response = await self.client.async_get_data(
                    f"?get_{self._device_type}_all={self._device_id}"
                )
                _LOGGER.debug(response)
                return response.get(f"{self._device_type}_{self._device_id}")
            if self._device_type == "counter":
                response = await self.client.async_get_data(
                    f"?get_{self._device_type}={self._device_id}"
                )
                _LOGGER.debug(response)
                return response.get(f"{self._device_type}_{self._device_id}")
            if self._device_type == "bat":
                response = await self.client.async_get_data(
                    f"?get_battery={self._device_id}"
                )
                _LOGGER.debug(response)
                return response.get(f"battery_{self._device_id}")
            if self._device_type == "pv":
                response = await self.client.async_get_data(
                    f"?get_{self._device_type}={self._device_id}"
                )
                _LOGGER.debug(response)
                return response.get(f"{self._device_type}_{self._device_id}")
            if self._device_type == "controller":
                response = await self.client.async_get_data(
                    "?get_lastlivevaluesjson&raw=true"
                )
                _LOGGER.debug(response)
                return response
            if self._device_type == "io":
                return await self._async_get_io_outputs()
            # Add other device types here in the future
            return {}
        except (
            OpenWB2MqttApiClientCommunicationError,
            OpenWB2MqttApiClientError,
        ) as exception:
            raise UpdateFailed(exception) from exception

    async def _async_get_total(self, base_type: str) -> dict:
        """Fetch aggregated 'total' values for pv/chargepoint via simpleAPI.

        Uses openWB's ?get_<type>_<metric>=total feature (openWB/core PR #3568)
        to sum a metric across all IDs of a component, e.g.
        ?get_pv_power=total&get_pv_imported=total&...
        """
        query = "&".join(f"get_{base_type}_{metric}=total" for metric in TOTAL_METRICS)
        response = await self.client.async_get_data(f"?{query}")
        _LOGGER.debug("Total response for %s: %s", base_type, response)

        if not isinstance(response, dict):
            return {}

        # Expected shape (matches other component responses): {"pv_total": {...}}
        data = response.get(f"{base_type}_total")
        if isinstance(data, dict):
            return data

        # Fallback: some simpleAPI versions may return the metrics flat, e.g.
        # {"pv_power": 1234, "pv_imported": 5678, ...} instead of nested under
        # "pv_total". Normalize that shape too so sensors keep working.
        flat_result = {}
        for metric in TOTAL_METRICS:
            flat_key = f"{base_type}_{metric}"
            if flat_key in response:
                flat_result[metric] = response[flat_key]
            elif metric in response:
                flat_result[metric] = response[metric]

        if flat_result:
            return flat_result

        _LOGGER.warning(
            "Unexpected response shape for %s total values, could not find "
            "'%s_total' or flat keys in: %s",
            base_type,
            base_type,
            response,
        )
        return {}

    async def _async_get_io_outputs(self) -> dict:
        """Fetch all digital+analog outputs of an IO device (e.g. Dimm & Control).

        Uses openWB's ?get_io_output_all=<ID|auto> feature (openWB/core PR #3568).
        Returns a flat dict of {output_name: value}, e.g. {"DO1": 0, "AO1": 45.5}.

        NOTE: The exact JSON response shape was not documented in the PR text, so
        this parses defensively (nested under "io_<id>" like other components, or
        already flat) and logs a warning if neither shape matches.
        """
        response = await self.client.async_get_data(
            f"?get_io_output_all={self._device_id}"
        )
        _LOGGER.debug("IO output response for id=%s: %s", self._device_id, response)

        if not isinstance(response, dict):
            return {}

        # Expected shape (matches other component responses): {"io_2": {...}}
        for key, value in response.items():
            if key.lower().startswith("io_") and isinstance(value, dict):
                return value

        # Fallback: response may already be flat, e.g. {"DO1": 0, "AO1": 45.5}
        # without an "io_<id>" wrapper. Filter out any obvious metadata keys
        # (success/message) that some simpleAPI endpoints add.
        flat_result = {
            k: v for k, v in response.items() if k not in ("success", "message")
        }
        if flat_result:
            return flat_result

        _LOGGER.warning(
            "Unexpected response shape for IO device id=%s, could not find "
            "an 'io_<id>' key or flat output keys in: %s",
            self._device_id,
            response,
        )
        return {}
