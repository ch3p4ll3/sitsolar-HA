"""Config flow, options flow, reconfigure and reauth tests."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
import pytest

from custom_components.sitsolar.const import (
    CONF_DONGLE_SERIAL,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from sitsolar_dongle import DongleError, EnvelopeError

from .conftest import TEST_DONGLE_SERIAL, TEST_HOST, TEST_INVERTER_SERIAL


async def test_user_flow_success(
    hass: HomeAssistant, dongle: Any
) -> None:
    """A successful probe creates the entry keyed on the inverter serial."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: TEST_HOST,
            CONF_DONGLE_SERIAL: TEST_DONGLE_SERIAL,
            CONF_SCAN_INTERVAL: 30,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # Title comes from the model register.
    assert result["title"] == "3K-HS"
    assert result["data"][CONF_HOST] == TEST_HOST
    assert result["data"][CONF_DONGLE_SERIAL] == TEST_DONGLE_SERIAL
    assert result["data"][CONF_SCAN_INTERVAL] == 30
    # unique_id is the inverter serial, not the dongle serial.
    assert result["result"].unique_id == TEST_INVERTER_SERIAL


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, dongle: Any
) -> None:
    """A transport failure surfaces cannot_connect and re-shows the form."""
    dongle.connect_exception = DongleError(
        f"Cannot connect to {TEST_HOST}:7078"
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: TEST_HOST,
            CONF_DONGLE_SERIAL: TEST_DONGLE_SERIAL,
            CONF_SCAN_INTERVAL: 30,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"][CONF_HOST] == "cannot_connect"


async def test_user_flow_invalid_auth(
    hass: HomeAssistant, dongle: Any
) -> None:
    """EnvelopeError (HMAC mismatch) surfaces invalid_auth."""
    dongle.connect_exception = EnvelopeError("HMAC mismatch (wrong serial number?)")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: TEST_HOST,
            CONF_DONGLE_SERIAL: TEST_DONGLE_SERIAL,
            CONF_SCAN_INTERVAL: 30,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"][CONF_DONGLE_SERIAL] == "invalid_auth"


async def test_user_flow_unknown_error(
    hass: HomeAssistant, dongle: Any
) -> None:
    """An unexpected error surfaces as 'unknown'."""
    dongle.connect_exception = DongleError("something odd happened")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: TEST_HOST,
            CONF_DONGLE_SERIAL: TEST_DONGLE_SERIAL,
            CONF_SCAN_INTERVAL: 30,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A second entry for the same inverter aborts already_configured."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            # Different host/serial, but the probe returns the same inverter.
            CONF_HOST: "192.0.2.50",
            CONF_DONGLE_SERIAL: "DGL9999999",
            CONF_SCAN_INTERVAL: 30,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_interval(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The options flow writes a new scan interval and reloads the entry."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 45}
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == 45
    assert mock_entry.options[CONF_SCAN_INTERVAL] == 45


async def test_options_flow_clamps_to_minimum(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """An interval below the 15 s minimum is rejected by the schema."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    # vol.Range(min=15) raises during schema validation on a too-small value.
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 5}
        )


async def test_reconfigure_changes_host(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Reconfigure probes the new host and updates the entry's host."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    new_host = "192.0.2.99"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": mock_entry.entry_id,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: new_host}
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data[CONF_HOST] == new_host


async def test_reauth_updates_serial(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Reauth probes the replacement serial and updates the entry."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    new_dongle_serial = "DGL7654321"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_entry.entry_id,
            "unique_id": mock_entry.unique_id,
        },
        data=dict(mock_entry.data),
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DONGLE_SERIAL: new_dongle_serial}
    )
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_entry.data[CONF_DONGLE_SERIAL] == new_dongle_serial
