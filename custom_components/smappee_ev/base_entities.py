from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmappeeCoordinator
from .helpers import make_device_info, make_unique_id, station_serial

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "SmappeeBaseEntity",
    "SmappeeStationEntity",
    "SmappeeConnectorEntity",
]


class SmappeeBaseEntity(CoordinatorEntity[SmappeeCoordinator]):
    """Common base providing station/connector id storage and device_info."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmappeeCoordinator, sid: int, station_uuid: str) -> None:
        super().__init__(coordinator)
        self._sid = sid
        self._station_uuid = station_uuid
        self._serial = station_serial(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        # 1. Use the serial for identification instead of parsing the UUID
        serial = self._serial

        # 2. Derive identity from your coordinator's collected data
        # (This is much more reliable than string-parsing a numeric ID)
        is_grid_monitor = "GRID_" in self._station_uuid.upper()

        # Determine model/type based on logic, not string parsing
        if is_grid_monitor:
            model_name = "Infinity Grid Monitor"
            device_type = "grid_monitor"
        else:
            model_name = "EV Wall"
            device_type = "charger_main"

        # 3. Build identifiers safely
        device_identifiers = {(DOMAIN, f"{self._sid}:{serial}:{self._station_uuid}")}

        # 4. Correctly fetch firmware from the object we successfully bound
        data = getattr(self.coordinator, "data", None)
        firmware = data.station.firmware_version if data and data.station else None

        return make_device_info(
            sid=self._sid,
            serial=serial,
            station_uuid=self._station_uuid,
            model=model_name,
            sw_version=firmware,
            via_device_uuid=None,
            device_identifiers=device_identifiers,
            device_name=f"Smappee {model_name} ({serial})",
            serial_number=serial,
        )

    async def async_added_to_hass(self) -> None:
        """Register coordinator update listeners to push dynamic device metadata."""
        await super().async_added_to_hass()

        # Voeg een listener toe die direct reageert als de coordinator updates pusht
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_update_device_registry_metadata)
        )
        # Voer hem direct uit voor het geval er al data klaarstaat
        self._async_update_device_registry_metadata()

    def _async_update_device_registry_metadata(self) -> None:
        """Safely push post-boot coordinator updates straight into the HA Device Registry."""
        data = getattr(self.coordinator, "data", None)
        if not data:
            return

        _LOGGER.debug("Received coordinator update, checking for device registry metadata changes...")
        firmware: str | None = None
        # Controleer of we data.station hebben (uit je log bleek dit een StationState object te zijn)
        _LOGGER.debug("Coordinator data content: %s", data)
        if hasattr(data, "station") and data.station:
            # Smappee API stopt firmware vaak in firmwareVersion of firmware_version
            firmware = getattr(data.station, "firmware_version", None) or getattr(data.station, "firmwareVersion", None)

        # Als er (nog) geen firmware is gevonden in de live data, hoeven we de registry niet te pushen
        if not firmware:
            return

        # Haal de device registry op en zoek ons specifieke apparaat op basis van de identifiers
        dev_reg = dr.async_get(self.hass)
        device_info_dict = self.device_info
        identifiers = device_info_dict.get("identifiers")

        if identifiers:
            device = dev_reg.async_get_device(identifiers=identifiers)
            if device and device.sw_version != firmware:
                _LOGGER.debug(
                    "[Smappee Dynamic Registry] Updating device %s firmware to: %s",
                    device.name, firmware
                )
                dev_reg.async_update_device(
                    device_id=device.id,
                    sw_version=firmware
                )


class SmappeeStationEntity(SmappeeBaseEntity):
    """Base for station-scope entities (no connector)."""

    def __init__(
        self,
        coordinator: SmappeeCoordinator,
        sid: int,
        station_uuid: str,
        unique_suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, sid, station_uuid)
        self._attr_unique_id = make_unique_id(sid, self._serial, station_uuid, None, unique_suffix)
        self._attr_name = name


class SmappeeConnectorEntity(SmappeeBaseEntity):
    """Base for connector-scope entities."""

    def __init__(
        self,
        coordinator: SmappeeCoordinator,
        sid: int,
        station_uuid: str,
        connector_uuid: str,
        unique_suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, sid, station_uuid)
        self._connector_uuid = connector_uuid
        # legacy compatibility attribute (existing code referenced _uuid)
        self._uuid = connector_uuid
        self._attr_unique_id = make_unique_id(
            sid, self._serial, station_uuid, connector_uuid, unique_suffix
        )
        self._attr_name = name

    # Convenience accessors
    @property
    def connector_uuid(self) -> str:
        return self._connector_uuid

    @property
    def _conn_state(self) -> Any | None:
        data = getattr(self.coordinator, "data", None)
        if not data:
            return None
        return (getattr(data, "connectors", None) or {}).get(self._connector_uuid)
