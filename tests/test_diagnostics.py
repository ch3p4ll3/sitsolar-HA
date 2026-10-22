"""Diagnostics redaction tests: host and serials must never leak."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from custom_components.sitsolar.const import CONF_DONGLE_SERIAL

from .conftest import TEST_DONGLE_SERIAL, TEST_HOST, TEST_INVERTER_SERIAL


async def test_diagnostics_redact_host_and_serials(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The dump must not contain the host, dongle serial or inverter serial."""
    from custom_components.sitsolar.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    # The entry's stored host and dongle serial are redacted.
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_DONGLE_SERIAL] == "**REDACTED**"

    # The dongle info's serial (the password) is redacted.
    dongle_info = diag["dongle"]["info"]
    assert dongle_info["serial"] == "**REDACTED**"

    # The inverter serial in device_info is redacted too.
    assert diag["device_info"]["serial_number"] == "**REDACTED**"

    # Nothing anywhere in the serialized dump leaks the raw secrets.
    dumped = json.dumps(diag)
    assert TEST_HOST not in dumped
    assert TEST_DONGLE_SERIAL not in dumped
    assert TEST_INVERTER_SERIAL not in dumped


async def test_diagnostics_includes_readings_and_coordinator(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The dump keeps the non-secret readings and coordinator health."""
    from custom_components.sitsolar.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["poll_latency_seconds"] is not None
    # A readable battery reading survives redaction (no serial-like keys).
    assert diag["readings"]["battery_soc"]["value"] == 64
    # The dongle is connected.
    assert diag["dongle"]["connected"] is True
