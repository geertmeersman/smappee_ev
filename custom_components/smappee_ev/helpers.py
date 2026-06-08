from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONFIGURATIN_URL, DOMAIN, MANUFACTURER


def make_device_info(
    sid: int,
    serial: str,
    station_uuid: str,
    model: str | None = None,
    sw_version: str | None = None,
    via_device_uuid: str | None = None,
    device_identifiers: set[tuple[str, str]] | None = None,
    device_name: str | None = None,
    serial_number: str | None = None,
) -> DeviceInfo:
    """Return a Home Assistant DeviceInfo object for a given station or sub-component.
    Accepts explicit identifiers and display names computed by the entity layer
    to prevent entity overlapping and device merging in the Home Assistant UI.
    """

    # Use the isolated identifiers and names provided by the entity if available
    identifiers = device_identifiers if device_identifiers else {(DOMAIN, f"{sid}:{serial}:{station_uuid}")}
    name = device_name if device_name else f"Smappee EV {serial}"

    device_info = DeviceInfo(
        identifiers=identifiers,
        name=name,
        manufacturer=MANUFACTURER,
        configuration_url=CONFIGURATIN_URL,
        serial_number=serial_number,
    )

    if model:
        device_info["model"] = model
    if sw_version:
        device_info["sw_version"] = sw_version

    # Establish a clean parent-child layout nested view in the HA UI if specified
    if via_device_uuid:
        device_info["via_device"] = (DOMAIN, f"{sid}:{serial}:{via_device_uuid}")

    return device_info


def make_unique_id(
    sid: int,
    serial: str,
    station_uuid: str,
    connector_uuid: str | None,
    metric: str,
) -> str:
    """
    Generate a globally unique ID for any entity.

    Args:
        sid: service location ID
        serial: station serial
        station_uuid: UUID of the station
        connector_uuid: UUID of the connector (None for station-wide entities)
        metric: suffix for the entity type, e.g. "mqtt_connected", "charging_mode"
    """
    if connector_uuid:
        return f"{sid}:{serial}:{station_uuid}:{connector_uuid}:{metric}"
    return f"{sid}:{serial}:{station_uuid}:{metric}"


# ----------------------------------------------------------------------------------
# Additional helpers to reduce duplication across entity platforms
# ----------------------------------------------------------------------------------


def station_serial(coord) -> str:
    """Return the station serial from a coordinator (fallback 'unknown')."""
    return getattr(getattr(coord, "station_client", None), "serial_id", "unknown")


def connector_state(coordinator, connector_uuid: str) -> Any | None:
    """Lookup a connector state object from coordinator data."""
    data = getattr(coordinator, "data", None)
    if not data:
        return None
    return (getattr(data, "connectors", None) or {}).get(connector_uuid)


def build_connector_label(api_client, connector_uuid: str) -> str:
    """Return a human friendly connector label (prefers numeric connector number)."""
    num = getattr(api_client, "connector_number", None)
    return f"Connector {num}" if num is not None else f"Connector {connector_uuid[-4:]}"


def update_total_increasing(last: float | None, candidate: float | None) -> float | None:
    """
    Enforce monotonic increasing semantics for total energy-like sensors.

    Rules:
      * If candidate is None -> keep last
      * If last exists and candidate < last or candidate == 0 -> keep last (guards resets)
      * Else accept candidate
    Returns the value to expose (which may be unchanged last).
    """
    if candidate is None:
        return last
    if last is not None and (candidate < last or candidate == 0):
        return last
    return candidate


def safe_sum(values) -> float | None:
    """
    Best effort sum of an iterable of numeric-like values, returning float or None.

    Accepts any list/tuple of values coercible to float. Returns None if empty or any
    element cannot be converted.
    """
    if not isinstance(values, list | tuple) or not values:  # type: ignore[arg-type]
        return None
    try:
        return float(sum(float(v) for v in values))
    except (TypeError, ValueError):  # any non-numeric
        return None


__all__ = [
    "make_device_info",
    "make_unique_id",
    "station_serial",
    "connector_state",
    "build_connector_label",
    "update_total_increasing",
    "safe_sum",
]
