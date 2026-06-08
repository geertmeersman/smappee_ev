from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import SmappeeApiClient
from .base_entities import SmappeeStationEntity
from .coordinator import SmappeeCoordinator
from .data import SmappeeEvConfigEntry
from .helpers import station_serial

_LOGGER = logging.getLogger(__name__)

def _station_serial(coord: SmappeeCoordinator) -> str:
    return station_serial(coord)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SmappeeEvConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = config_entry.runtime_data
    sites = runtime.sites

    entities: list[SmappeeStationEntity] = []
    for sid, site in (sites or {}).items():
        stations = (site or {}).get("stations", {})
        for st_uuid, bucket in (stations or {}).items():
            coord: SmappeeCoordinator = bucket["coordinator"]
            st_client: SmappeeApiClient = bucket["station_client"]
            conns: dict[str, SmappeeApiClient] = bucket.get("connector_clients", {})
            entities.append(SmappeeMqttConnectivity(coord, st_client, sid, st_uuid))

            # GUARD: Only append hardware car pilot-wire sensing loops to the charger
            if "GRID_" in st_uuid.upper():
                continue

            for cuuid, client in (conns or {}).items():
                # Append station sensors for each connector
                _LOGGER.debug(
                    "Placeholder for future connector-level binary sensors on station %s connector %s",
                    sid,
                    st_uuid,
                )
    async_add_entities(entities, True)


class SmappeeMqttConnectivity(SmappeeStationEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "MQTT Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmappeeCoordinator,
        api_client: SmappeeApiClient,
        sid: int,
        station_uuid: str,
    ) -> None:
        SmappeeStationEntity.__init__(
            self,
            coordinator,
            sid,
            station_uuid,
            unique_suffix="mqtt_connected",
            name="MQTT Connected",
        )
        self.api_client = api_client

    @property
    def device_info(self):
        return super().device_info

    @property
    def is_on(self) -> bool:
        st = self.coordinator.data.station if self.coordinator.data else None
        return bool(getattr(st, "mqtt_connected", False))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "service_location_id": self._sid,
            "station_serial": self._serial,
            "station_uuid": self._station_uuid,
        }
