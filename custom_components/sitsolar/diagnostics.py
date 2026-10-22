"""Diagnostics support for the Sit Solar integration.

The dongle serial number doubles as the cryptographic password that
authenticates every frame, so it is redacted along with the host address and
any register key that carries a serial number.  The inverter serial number is
not secret (Home Assistant shows it on the device page) but is redacted here
anyway to keep the dump shareable.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.redact import async_redact_data

from . import SitSolarConfigEntry
from .const import CONF_DONGLE_SERIAL

TO_REDACT: set[str] = {
    CONF_HOST,
    CONF_DONGLE_SERIAL,
    # DongleInfo field name (the password) ...
    "serial",
    # ... and the register keys that carry serial numbers.
    "serial_number",
}


def _json_safe_device_info(device_info: DeviceInfo) -> dict[str, Any]:
    """Return *device_info* JSON-safe, with serial-bearing fields redacted.

    The device identifiers are ``(domain, inverter_serial)`` tuples, so the
    serial is masked there as well as in the ``serial_number`` field.
    """
    safe: dict[str, Any] = dict(device_info)
    identifiers = safe.get("identifiers")
    if isinstance(identifiers, set):
        safe["identifiers"] = sorted(
            (domain, "**REDACTED**") for domain, *_ in identifiers
        )
    connections = safe.get("connections")
    if isinstance(connections, set):
        safe["connections"] = sorted(connections)
    return async_redact_data(safe, TO_REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SitSolarConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, with secrets redacted."""
    coordinator = entry.runtime_data
    data = coordinator.data
    poll = data.poll if data else {}

    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_update": data.updated_at.isoformat() if data else None,
            "poll_latency_seconds": round(data.latency, 3) if data else None,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "alive_counters": sorted(coordinator.alive_counters),
        },
        "dongle": {
            "connected": coordinator.client.connected,
            "info": async_redact_data(
                asdict(coordinator.client.info),
                TO_REDACT,
            )
            if coordinator.client.info is not None
            else None,
        },
        "device_info": _json_safe_device_info(coordinator.device_info),
        "unreadable_ranges": data.unreadable if data else [],
        "readings": async_redact_data(dict(poll), TO_REDACT),
    }
    return diagnostics
