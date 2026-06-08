import asyncio
import logging
from typing import cast

from aiohttp import ClientError, ClientSession
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api_client import SmappeeApiClient
from .const import BASE_URL, CONF_PASSWORD, DOMAIN, UPDATE_INTERVAL_DEFAULT
from .coordinator import SmappeeCoordinator
from .data import RuntimeData, SmappeeEvConfigEntry
from .mqtt_gateway import SmappeeMqtt
from .oauth import OAuth2Client, SmappeeAuthError
from .services import register_services, unregister_services

_LOGGER = logging.getLogger(__name__)
_SERVICE_REGISTRATION_SENTINEL = "start_charging"
PLATFORMS = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
]

# Allow multiple parallel updates per platform (entities rely on single coordinator)
PARALLEL_UPDATES = 0

CONFIG_SCHEMA = cv.platform_only_config_schema(DOMAIN)

# -------------------------
# Helpers for discovery
# -------------------------


def _is_station(dev: dict) -> bool:
    """True if device is a CHARGINGSTATION smartdevice."""
    t = dev.get("type")
    if isinstance(t, dict):
        return (t.get("category") or "").upper() == "CHARGINGSTATION"
    return (dev.get("type") or "").upper() == "CHARGINGSTATION"


def _is_connector(dev: dict) -> bool:
    """True if device is a CARCHARGER smartdevice."""
    t = dev.get("type")
    if isinstance(t, dict):
        return (t.get("category") or "").upper() == "CARCHARGER"
    return (dev.get("type") or "").upper() == "CARCHARGER"


def _safe_str(val) -> str | None:
    """Convert to stripped string or None if not possible."""
    try:
        s = str(val)
    except (TypeError, ValueError):
        return None
    return s.strip() or None


def _find_in(dev: dict, *keys: str) -> str | None:
    """Try to discover a 'serialNumber' (or similar) inside a smartdevice."""
    # direct
    for k in keys:
        if k in dev and _safe_str(dev[k]):
            return _safe_str(dev[k])
    # scan configuration/properties for a field that looks like a serial
    for bag in ("configurationProperties", "properties"):
        for prop in dev.get(bag, []) or []:
            spec = prop.get("spec") or {}
            name = (spec.get("name") or "").lower()
            if "serial" in name:
                v = prop.get("value")
                if isinstance(v, dict):
                    v = v.get("value")
                if _safe_str(v):
                    return _safe_str(v)
    return None


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entry versions to the current format.

    Flow VERSION = 5.
    Version history relevant here:
      - v4 (and earlier) could still persist an 'update_interval' in data/options.
      - v5 removes user control of update interval (internal only) and drops that field.
    We migrate incrementally so users can skip versions safely.
    """
    version = entry.version
    data = dict(entry.data)
    options = dict(entry.options)

    updated = False

    # v5 cleanup: remove legacy 'update_interval' key if present (from v4 or earlier)
    if version < 5:
        if "update_interval" in data:
            data.pop("update_interval")
            updated = True
        if "update_interval" in options:
            options.pop("update_interval")
            updated = True
        version = 5

    if data.get("refresh_token") and CONF_PASSWORD in data:
        data.pop(CONF_PASSWORD)
        updated = True

    if updated or version != entry.version:
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=version)
        _LOGGER.info("Smappee EV config entry %s migrated to version %s", entry.entry_id, version)
    else:
        _LOGGER.debug(
            "Smappee EV config entry %s already at latest version %s", entry.entry_id, version
        )
    return True


async def _discover_service_locations(
    session: ClientSession, oauth_client: OAuth2Client
) -> list[dict]:
    """Return all service locations that have a deviceSerialNumber."""
    await oauth_client.ensure_token_valid()
    headers = {
        "Authorization": f"Bearer {oauth_client.access_token}",
        "Content-Type": "application/json",
    }
    resp = await session.get(f"{BASE_URL}/servicelocation", headers=headers)
    if resp.status != 200:
        text = await resp.text()
        raise RuntimeError(f"/servicelocation failed: {resp.status} - {text}")
    data = await resp.json()
    locations = data.get("serviceLocations", []) if isinstance(data, dict) else (data or [])
    return [sl for sl in locations if sl.get("deviceSerialNumber")]


# prepare site helpers
async def _fetch_devices(
    session: ClientSession, oauth_client: OAuth2Client, sid: int
) -> list[dict] | None:
    await oauth_client.ensure_token_valid()
    headers = {
        "Authorization": f"Bearer {oauth_client.access_token}",
        "Content-Type": "application/json",
    }
    resp = await session.get(f"{BASE_URL}/servicelocation/{sid}/smartdevices", headers=headers)
    if resp.status != 200:
        _LOGGER.warning(
            "GET smartdevices for %s failed: %s - %s", sid, resp.status, await resp.text()
        )
        return None
    return await resp.json()


def _split_devices(devices: list[dict]) -> tuple[list[dict], list[dict]]:
    stations = [d for d in (devices or []) if _is_station(d)]
    cars = [d for d in (devices or []) if _is_connector(d)]
    return stations, cars


async def _fetch_metering_cfg(
    oauth_client, session, sid, serial_str, station_devs
) -> dict[str, dict]:
    """Return {station_serial: {connectors{uuid:{id,position}}}}"""
    try:
        tmp_client = SmappeeApiClient(
            oauth_client,
            serial_str,
            _safe_str(station_devs[0].get("uuid")) or "station",
            _safe_str(station_devs[0].get("id")) or "0",
            sid,
            session=session,
            is_station=True,
        )
        cfg = await tmp_client.async_get_metering_configuration()
    except (ClientError, ValueError, KeyError, TimeoutError) as err:
        _LOGGER.warning("Failed to parse meteringconfiguration for %s: %s", sid, err)
        return {}

    out: dict[str, dict] = {}
    for st in (cfg or {}).get("chargingStations", []) or []:
        st_serial = _safe_str(st.get("serialNumber")) or _safe_str(st.get("serial"))
        if not st_serial:
            continue
        # Only stations at this site
        # -id == sid
        # or connecSerialNumber == deviceserialNumber
        st_id = _safe_str(st.get("id"))
        connect_sn = _safe_str(st.get("connectSerialNumber"))
        if not (st_id == _safe_str(sid) or connect_sn == serial_str):
            continue
        bucket = out.setdefault(st_serial, {"connectors": {}})
        for chg in st.get("chargers", []) or []:
            cuuid = _safe_str(chg.get("uuid"))
            if not cuuid:
                continue
            bucket["connectors"][cuuid] = {
                "id": _safe_str(chg.get("id")) or _safe_str(chg.get("smartDeviceId")),
                "position": chg.get("position"),
            }
    return out


def _make_station_clients(
    oauth_client, serial_str, sid, session, station_devs: list[dict]
) -> dict[str, dict]:
    stations: dict[str, dict] = {}
    for sd in station_devs:
        st_uuid = _safe_str(sd.get("uuid"))
        st_id = _safe_str(sd.get("id"))
        if not st_uuid or not st_id:
            continue

        st_serial = _find_in(sd, "serialNumber", "serial") or st_uuid
        st_client = SmappeeApiClient(
            oauth_client, serial_str, st_uuid, st_id, sid, session=session, is_station=True
        )
        stations[st_uuid] = {
            "station_client": st_client,
            "connector_clients": {},
            "coordinator": None,
            "mqtt": None,
            "serial": st_serial,
        }
    return stations


def _assign_connectors(stations, car_devs, mapping, oauth_client, serial_str, sid, session):
    for bucket in stations.values():
        st_serial = bucket.get("serial")
        if not st_serial or st_serial not in mapping:
            continue
        for cuuid, info in mapping[st_serial]["connectors"].items():
            src = next((d for d in car_devs if _safe_str(d.get("uuid")) == cuuid), None)
            if not src:
                continue
            cid = _safe_str(src.get("id")) or info.get("id") or "0"
            bucket["connector_clients"][cuuid] = SmappeeApiClient(
                oauth_client,
                serial_str,
                cuuid,
                cid,
                sid,
                session=session,
                connector_number=info.get("position")
                or src.get("connectorNumber")
                or src.get("position")
                or 1,
                charging_station_serial=st_serial,
            )


def _fallback_assign(
    stations: dict[str, dict],
    car_devs: list[dict],
    oauth_client: OAuth2Client,
    serial_str: str,
    sid: int,
    session: ClientSession,
) -> None:
    """Assign all remaining connectors to the first station if no mapping was found."""
    if not car_devs:
        return

    total_assigned = sum(len(b["connector_clients"]) for b in stations.values())
    if total_assigned > 0:
        return

    first_uuid = next(iter(stations.keys()), None)
    if not first_uuid:
        return

    st_serial = stations.get(first_uuid, {}).get("serial")
    _LOGGER.warning(
        "Could not map connectors to stations at %s; assigning all to first station", sid
    )

    subset = {}
    for d in car_devs:
        cuuid = _safe_str(d.get("uuid"))
        cid = _safe_str(d.get("id"))
        if not cuuid or not cid:
            continue
        subset[cuuid] = SmappeeApiClient(
            oauth_client,
            serial_str,
            cuuid,
            cid,
            sid,
            session=session,
            connector_number=d.get("connectorNumber") or d.get("position") or 1,
            charging_station_serial=st_serial,
        )
    stations[first_uuid]["connector_clients"] = subset


async def _create_coordinators(hass, stations, update_interval, config_entry=None):
    for bucket in stations.values():
        coord = SmappeeCoordinator(
            hass,
            station_client=bucket["station_client"],
            connector_clients=bucket["connector_clients"],
            update_interval=update_interval,
            config_entry=config_entry,
        )
        await coord.async_config_entry_first_refresh()
        bucket["coordinator"] = coord


def _setup_mqtt(
    hass, suuid, serial_str, sid, stations, client_id_prefix: str
) -> SmappeeMqtt | None:
    if not suuid:
        _LOGGER.warning("No serviceLocationUuid for %s; MQTT disabled for this site", sid)
        return None

    def _on_props(topic: str, payload: dict) -> None:
        for bucket in stations.values():
            coord = bucket.get("coordinator")
            if coord:
                coord.apply_mqtt_properties(topic, payload)

    def _on_conn(up: bool) -> None:
        for bucket in stations.values():
            coord = bucket.get("coordinator")
            if coord:
                coord.apply_mqtt_connection_change(up)

    mqtt = SmappeeMqtt(
        service_location_uuid=suuid,
        client_id=f"{client_id_prefix}-{sid}",
        serial_number=serial_str,
        on_properties=_on_props,
        service_location_id=sid,
        on_connection_change=_on_conn,
    )
    hass.async_create_task(mqtt.start())

    # disable polling if MQTT is active
    for b in stations.values():
        coord = b.get("coordinator")
        if coord:
            coord.update_interval = None
    return mqtt


async def _prepare_site(
    hass: HomeAssistant,
    session: ClientSession,
    oauth_client: OAuth2Client,
    sl: dict,
    update_interval: int,
    client_id_prefix: str,
    processed_uuids: set[str],
    config_entry: SmappeeEvConfigEntry | None = None,
) -> tuple[dict[str, dict] | None, SmappeeMqtt | None]:
    """Build coordinators, station/connector clients and MQTT for one service location."""

    sid = sl["serviceLocationId"]
    suuid = sl.get("serviceLocationUuid")
    serial_str = (sl.get("deviceSerialNumber") or "").strip()
    site_name = (sl.get("name") or "").strip()

    _LOGGER.debug("[Smappee EV] Booting integration infrastructure for profile: %s", site_name)

    if not serial_str:
        return None, None

    # Determine if this profile represents a dedicated grid/mains monitor
    is_grid_profile = "WALL" not in site_name.upper() and "CHARGER" not in site_name.upper()

    if is_grid_profile:
        # PURE GRID MONITOR INFRASTRUCTURE:
        # Do not fetch or process any smartdevices or charger assets.
        # Create a clean virtual station list using the location's true native parameters.
        _LOGGER.debug("[Smappee EV] Configuring grid profile tracker for site %s (%s)", site_name, serial_str)

        # We model the grid box under a clean local grid-specific unique token
        grid_uuid = f"grid_{serial_str}"
        st_client = SmappeeApiClient(
            oauth_client, serial_str, grid_uuid, str(sid), sid, session=session, is_station=True
        )
        stations = {
            grid_uuid: {
                "station_client": st_client,
                "connector_clients": {},
                "coordinator": None,
                "mqtt": None,
                "serial": serial_str,
            }
        }
    else:
        # PURE EV CHARGER INFRASTRUCTURE:
        devices = await _fetch_devices(session, oauth_client, sid)
        if devices is None:
            return None, None

        station_devs, car_devs = _split_devices(devices)
        display_station_devs = station_devs if station_devs else (devices[:1] if devices else [])
        if not display_station_devs:
            return None, None

        station_serial_to_connectors = await _fetch_metering_cfg(
            oauth_client, session, sid, serial_str, display_station_devs
        )

        stations = _make_station_clients(oauth_client, serial_str, sid, session, display_station_devs)
        _assign_connectors(stations, car_devs, station_serial_to_connectors, oauth_client, serial_str, sid, session)
        _fallback_assign(stations, car_devs, oauth_client, serial_str, sid, session)

    # Spawn your data streams
    await _create_coordinators(hass, stations, update_interval, config_entry=config_entry)
    mqtt = _setup_mqtt(hass, suuid, serial_str, sid, stations, client_id_prefix)

    for b in stations.values():
        b["mqtt"] = mqtt

    return stations, mqtt


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Smappee EV component."""
    hass.data.setdefault(DOMAIN, {})
    # Register services once domain-wide (multi-entry safe)
    if not hass.services.has_service(DOMAIN, _SERVICE_REGISTRATION_SENTINEL):
        await register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmappeeEvConfigEntry) -> bool:
    """Set up a Smappee EV account entry that discovers all service locations with a charger."""
    _LOGGER.debug("Setting up Smappee EV account entry: %s", entry.title)

    hass.data.setdefault(DOMAIN, {})

    # Use HA's aiohttp session
    session: ClientSession = async_get_clientsession(hass)

    update_interval = UPDATE_INTERVAL_DEFAULT

    def _store_tokens(tokens: dict[str, object]) -> None:
        data = dict(entry.data)
        data.update(tokens)
        if data.get("refresh_token"):
            data.pop(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=data)

    oauth_client = OAuth2Client(entry.data, session=session, token_update_callback=_store_tokens)

    # 1) Discover sites
    try:
        with_serial = await _discover_service_locations(session, oauth_client)
    except SmappeeAuthError as err:
        raise ConfigEntryAuthFailed(f"Auth failed: {err}") from err
    except (ClientError, RuntimeError, ValueError) as err:
        # Authentication / authorization problems should trigger reauth
        if getattr(oauth_client, "access_token", None) is None:
            raise ConfigEntryAuthFailed(f"Auth failed: {err}") from err
        _LOGGER.debug("Transient error loading service locations: %s", err)
        raise ConfigEntryNotReady(f"Loading service locations failed: {err}") from err
    if not with_serial:
        _LOGGER.debug("No service locations with deviceSerialNumber found (retry later)")
        raise ConfigEntryNotReady("No service locations with deviceSerialNumber found")

    sites: dict[int, dict] = {}
    mqtt_clients: dict[int, SmappeeMqtt] = {}
    processed_smart_device_uuids: set[str] = set()

    # Sorteer zodat laadpalen eerst starten (voor de juiste MQTT luisteraar)
    with_serial.sort(key=lambda x: "WALL" in (x.get("name") or "").upper(), reverse=True)

    # 2) Prepare each site in parallel
    client_id_prefix = f"ha-{entry.entry_id[-6:]}"

    for sl in with_serial:
        sid = sl.get("serviceLocationId")
        try:
            res = await _prepare_site(
                hass,
                session,
                oauth_client,
                sl,
                update_interval,
                client_id_prefix,
                processed_smart_device_uuids,
                config_entry=entry,
            )
        except asyncio.CancelledError:
            raise
        except Exception as res_err:
            _LOGGER.warning("Site %s initialization failed: %s", sid, res_err)
            continue

        if not res:
            continue

        stations_map, mqtt = res
        if not stations_map:
            continue

        if mqtt:
            mqtt_clients[sid] = mqtt
        sites[sid] = {
            "stations": stations_map,
            "name": sl.get("name"),
            "serviceLocationUuid": sl.get("serviceLocationUuid"),
            "deviceSerialNumber": sl.get("deviceSerialNumber"),
        }

    if not sites:
        raise ConfigEntryNotReady("Smappee API layout mapping failed")

    runtime = RuntimeData(
        api=oauth_client,
        sites=sites,
        mqtt=cast(dict[int, object], mqtt_clients),
    )
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmappeeEvConfigEntry) -> bool:
    """Unload a Smappee EV config entry clean."""
    try:
        rd = entry.runtime_data
    except AttributeError:
        pass
    else:
        if isinstance(rd, RuntimeData):
            for sid, mqtt in (rd.mqtt or {}).items():
                stop_fn = getattr(mqtt, "stop", None)
                if not callable(stop_fn):
                    continue
                try:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    _LOGGER.warning("Failed terminating MQTT listener: %s", err)

            for site in (rd.sites or {}).values():
                for bucket in site.get("stations", {}).values():
                    coord = bucket.get("coordinator")
                    if coord and hasattr(coord, "async_shutdown"):
                        try:
                            await coord.async_shutdown()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            pass

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        active_entries = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
        ]
        if not active_entries and hass.services.has_service(DOMAIN, _SERVICE_REGISTRATION_SENTINEL):
            await unregister_services(hass)
            hass.data.pop(DOMAIN, None)
    return unload_ok


def _current_station_device_identifiers(entry: SmappeeEvConfigEntry) -> set[str]:
    """Return Smappee EV device identifiers currently known for this entry."""
    try:
        rd = entry.runtime_data
    except AttributeError:
        return set()
    if not isinstance(rd, RuntimeData):
        return set()

    identifiers: set[str] = set()
    for sid, site in (rd.sites or {}).items():
        for station_uuid, bucket in (site.get("stations") or {}).items():
            serial = bucket.get("serial")
            if not serial:
                station_client = bucket.get("station_client")
                serial = getattr(station_client, "serial_id", None) or getattr(station_client, "serial", None)
            if serial:
                identifiers.add(f"{sid}:{serial}:{station_uuid}")
    return identifiers


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SmappeeEvConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow users to remove stale Smappee EV devices from the registry."""
    domain_identifiers = {identifier for domain, identifier in device_entry.identifiers if domain == DOMAIN}
    if not domain_identifiers:
        return True
    current_identifiers = _current_station_device_identifiers(entry)
    if not current_identifiers:
        return False
    return domain_identifiers.isdisjoint(current_identifiers)
