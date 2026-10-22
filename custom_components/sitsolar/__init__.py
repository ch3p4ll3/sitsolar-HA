"""The Sit Solar inverter (Si-Dongle-L) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from sitsolar_dongle import AsyncDongleClient

from .const import CONF_DONGLE_SERIAL, PLATFORMS, REQUEST_TIMEOUT
from .coordinator import SitSolarCoordinator

_LOGGER = logging.getLogger(__name__)

type SitSolarConfigEntry = ConfigEntry[SitSolarCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SitSolarConfigEntry) -> bool:
    """Set up one config entry: a single client behind a single coordinator."""
    client = AsyncDongleClient(
        entry.data[CONF_HOST],
        entry.data[CONF_DONGLE_SERIAL],
        timeout=REQUEST_TIMEOUT,
    )
    coordinator = SitSolarCoordinator(hass, entry, client)
    # The async client runs a background reader for as long as it is
    # connected; close() is mandatory on unload or the dongle keeps serving
    # that connection and locks every other client out until reboot.
    entry.async_on_unload(client.close)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SitSolarConfigEntry) -> bool:
    """Unload a config entry (the client close runs via async_on_unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
