"""Setup, unload, failure and recovery tests."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
import pytest

from custom_components.sitsolar.const import DOMAIN
from sitsolar_dongle import DongleError, EnvelopeError

from .conftest import TEST_HOST, state_for


async def test_setup_creates_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A successful first poll sets up the entry and creates core entities."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.LOADED
    client = dongle.client
    assert client.connected is True
    assert client.host == TEST_HOST

    soc = state_for(hass, "sensor", "battery_soc")
    assert soc is not None
    assert soc.state == "64"
    assert soc.attributes["unit_of_measurement"] == "%"

    pv = state_for(hass, "sensor", "total_pv_power")
    assert pv is not None
    # 3.45 kW reported as 3450 W
    assert float(pv.state) == pytest.approx(3450.0)

    problem = state_for(hass, "binary_sensor", "problem")
    assert problem is not None
    assert problem.state == "off"


async def test_setup_single_phase_skips_three_phase_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """device_type 0x21 (single phase) must not create S/T-phase entities."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "phase_r_voltage") is not None
    assert state_for(hass, "sensor", "phase_s_voltage") is None
    assert state_for(hass, "sensor", "phase_t_current") is None
    assert state_for(hass, "sensor", "meter_s_power") is None
    assert state_for(hass, "sensor", "eps_voltage_s") is None


async def test_setup_three_phase_creates_phase_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """device_type 0x23 (three phase) creates the S/T-phase entities."""
    from .conftest import make_poll

    dongle.poll_result = make_poll(overrides={"device_type": 35})
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_entry.runtime_data
    assert coordinator.is_three_phase is True
    assert state_for(hass, "sensor", "phase_s_voltage") is not None
    assert state_for(hass, "sensor", "phase_t_current") is not None


async def test_wrong_serial_raises_auth_failed(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """EnvelopeError (HMAC mismatch) must surface as an auth failure."""
    dongle.connect_exception = EnvelopeError("HMAC mismatch (wrong serial number?)")
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR


async def test_unreachable_host_sets_up_retry(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A transport failure during setup must not be fatal; HA retries later."""
    dongle.connect_exception = DongleError("Cannot connect to 192.0.2.1:7078")
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_poll_failure_then_recovery(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """After a failed poll the entities go unavailable, then recover."""
    from sitsolar_dongle import ModbusError

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_entry.runtime_data

    assert state_for(hass, "sensor", "battery_soc").state == "64"

    # A ModbusError on a healthy connection is not fatal and arms no backoff.
    dongle.client.poll_exception = ModbusError(0x02, 0x01, 0x03)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert state_for(hass, "sensor", "battery_soc").state == "unavailable"

    # The exception clears and the very next poll recovers.
    dongle.client.poll_exception = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert state_for(hass, "sensor", "battery_soc").state == "64"


async def test_reconnect_lockout_survives_ha_retry_after(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The lockout must keep working after HA consumes ``retry_after``.

    HA's DataUpdateCoordinator stores ``UpdateFailed.retry_after`` in its own
    ``_retry_after`` attribute and resets it to None as soon as it has
    scheduled the next refresh.  Sharing that attribute name used to make
    the second lockout tick crash with ``TypeError: unsupported operand
    type(s) for -: 'NoneType' and 'float'``.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_entry.runtime_data
    client = dongle.client
    assert client.connect_calls == 1

    # The connection drops: the next connect attempt fails like the real
    # "Connection reset by peer" does.
    client.connected = False
    client.connect_exception = DongleError("connection lost: [Errno 104]")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    # The failed connect attempt armed the backoff.
    assert client.connect_calls == 2

    # The next tick lands inside the lockout window: it must fail with
    # UpdateFailed, not with a TypeError, and must not try to reconnect.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert "lockout" in str(coordinator.last_exception)
    assert client.connect_calls == 2

    # Once the deadline passes the coordinator reconnects and recovers.
    coordinator._reconnect_deadline = 0.0
    client.connect_exception = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert client.connect_calls == 3
    assert state_for(hass, "sensor", "battery_soc").state == "64"


async def test_unreadable_block_hides_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A block that refuses to read must not create its entities."""
    from .conftest import make_poll

    dongle.poll_result = make_poll(
        unreadable_sections=("EPS_parameters", "meter_parameters")
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_entry.runtime_data
    assert len(coordinator.data.unreadable) == 2

    assert state_for(hass, "sensor", "eps_voltage_r") is None
    assert state_for(hass, "sensor", "eps_total_p") is None
    assert state_for(hass, "sensor", "meter_r_voltage") is None
    assert state_for(hass, "sensor", "energy_to_grid_total") is None
    # Entities from readable blocks still exist.
    assert state_for(hass, "sensor", "battery_soc") is not None


async def test_zero_lifetime_counter_is_unavailable(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A lifetime counter stuck at 0 stays unavailable, not total_increasing."""
    from .conftest import make_poll

    # energy_from_pv_total stays at its default; battery_cycles starts at 0.
    dongle.poll_result = make_poll(overrides={"battery_cycles": 0})
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    cycles = state_for(hass, "sensor", "battery_cycles")
    assert cycles is not None
    assert cycles.state == "unavailable"

    # A nonzero lifetime counter is reported normally.
    assert state_for(hass, "sensor", "energy_from_pv_total").state == "5123.4"


async def test_unload_closes_client(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Unloading the entry must close the dongle connection."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    client = dongle.client
    assert client.closed is False

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert client.closed is True
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


async def test_duplicate_inverter_aborts(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A second entry for the same inverter serial must abort."""

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant import config_entries

    from custom_components.sitsolar.const import CONF_DONGLE_SERIAL, CONF_SCAN_INTERVAL

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "192.0.2.50",
            CONF_DONGLE_SERIAL: "DGL9999999",
            CONF_SCAN_INTERVAL: 30,
        },
    )
    await hass.async_block_till_done()

    # The probe returns the same inverter serial, so the flow aborts on the
    # unique_id derived from it.
    assert result["reason"] == "already_configured"
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
