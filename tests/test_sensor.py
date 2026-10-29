"""Sensor value tests: computed sensors, enum maps, quirks, alarm decoding."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest

from .conftest import make_poll, state_for


async def test_computed_apparent_power_and_power_factor(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """apparent = sqrt(P^2+Q^2), PF = P/apparent (register values are unusable)."""
    # P=417, Q=210 -> apparent = sqrt(417^2 + 210^2) = 466.89
    #                     PF      = 417 / 466.89       = 0.8932
    dongle.poll_result = make_poll(
        overrides={"inverter_p": 417, "inverter_q": 210}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    apparent = state_for(hass, "sensor", "apparent_power_calc")
    assert float(apparent.state) == pytest.approx(466.893, abs=1e-3)

    pf = state_for(hass, "sensor", "power_factor_calc")
    assert float(pf.state) == pytest.approx(0.89316, abs=1e-3)

    # The raw register-backed quirk entities exist but are disabled diagnostics
    # (disabled-by-default entities have no state until enabled).
    from homeassistant.helpers import entity_registry as er

    reg_entry = er.async_get(hass).async_get_entity_id(
        "sensor", "sitsolar", "INV1234567_apparent_power"
    )
    assert reg_entry is not None
    entry = er.async_get(hass).async_get(reg_entry)
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_computed_grid_exchange_and_home_consumption(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """grid_exchange inverts meter sign; home = PV + battery - meter (W)."""
    # meter_total_p = -215 (importing 215 W) -> grid_exchange = +215 (import).
    # home = 3.45 kW * 1000 + (-197) - (-215) = 3450 - 197 + 215 = 3468 W.
    dongle.poll_result = make_poll(
        overrides={
            "total_pv_power": 3.45,
            "battery_power": -197,
            "meter_total_p": -215,
        }
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    exchange = state_for(hass, "sensor", "grid_exchange_power")
    assert float(exchange.state) == pytest.approx(215.0)

    home = state_for(hass, "sensor", "home_consumption")
    assert float(home.state) == pytest.approx(3468.0)
    # The estimate carries an explanatory note attribute.
    assert "estimate" in home.attributes["note"]


async def test_battery_charge_discharge_split(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Signed battery power/current split into nonnegative charge & discharge."""
    # Discharging: power +197, current +3.7 -> discharge entities nonzero.
    dongle.poll_result = make_poll(
        overrides={"battery_power": 197, "battery_current": 3.7}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert float(state_for(hass, "sensor", "battery_discharge_power").state) == 197.0
    assert float(state_for(hass, "sensor", "battery_charge_power").state) == 0.0
    discharge_current = state_for(hass, "sensor", "battery_discharge_current")
    assert float(discharge_current.state) == pytest.approx(3.7)
    assert float(state_for(hass, "sensor", "battery_charge_current").state) == 0.0
    assert state_for(hass, "sensor", "battery_flow").state == "discharging"

    # Charging: power -197, current -3.7 -> charge entities nonzero.
    # NOTE: FakeDongleFactory copies poll_result into the client at
    # construction, so we mutate the live client's poll_result directly.
    dongle.client.poll_result = make_poll(
        overrides={"battery_power": -197, "battery_current": -3.7}
    )
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert float(state_for(hass, "sensor", "battery_charge_power").state) == 197.0
    assert float(state_for(hass, "sensor", "battery_discharge_power").state) == 0.0
    assert state_for(hass, "sensor", "battery_flow").state == "charging"

    # Idle: power 0 -> flow idle.
    dongle.client.poll_result = make_poll(overrides={"battery_power": 0})
    await mock_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state_for(hass, "sensor", "battery_flow").state == "idle"


async def test_enum_maps(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Register enum codes map to translated option slugs."""
    # operation_status 2 = normal, battery_status 2 = discharging, bms_status 1 = ok.
    dongle.poll_result = make_poll(
        overrides={"operation_status": 2, "battery_status": 2, "bms_status": 1}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "operation_status").state == "normal"
    assert state_for(hass, "sensor", "battery_status").state == "discharging"
    assert state_for(hass, "sensor", "bms_status").state == "ok"


async def test_meter_current_scale_correction(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """meter_r_current is corrected 0.1 -> 0.01 (factor 0.1)."""
    # Raw decode of 14.2 A at the protocol's 0.1 scale is 142; corrected is 14.2.
    # The DEFAULT_POLL_VALUES supply 1.42 A -> factor 0.1 yields 0.142.
    dongle.poll_result = make_poll(overrides={"meter_r_current": 14.2})
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    # 14.2 was encoded at scale 0.1 -> raw 142; native 14.2 * 0.1 = 1.42.
    current = state_for(hass, "sensor", "meter_r_current")
    assert float(current.state) == pytest.approx(1.42)
    assert "scale corrected" in current.attributes["note"]


async def test_alarm_status_and_problem_binary_sensor(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """DSP warning bit decodes into the alarm list and flips 'problem'."""
    # dsp_warning1 bit 0 set (code 201 = AC over current), dsp_warning5 = 200 (none).
    dongle.poll_result = make_poll(
        overrides={"dsp_warning1": 1, "dsp_warning5": 200}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    alarm = state_for(hass, "sensor", "alarm_status")
    assert alarm.state != "OK"
    assert "AC over current" in alarm.state
    # The active alarm list rides along as an attribute.
    assert alarm.attributes["active_alarms"]

    problem = state_for(hass, "binary_sensor", "problem")
    assert problem.state == "on"


async def test_no_alarm_when_clean(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """All-clear poll reports 'OK' and problem off."""
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "alarm_status").state == "OK"
    assert state_for(hass, "binary_sensor", "problem").state == "off"


async def test_battery_forbidden_binary_sensors(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """forbid_charge / forbid_discharge flags map to on/off binary sensors."""
    dongle.poll_result = make_poll(
        overrides={"forbid_charge": 1, "forbid_discharge": 0}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "binary_sensor", "battery_charge_forbidden").state == "on"
    assert (
        state_for(hass, "binary_sensor", "battery_discharge_forbidden").state == "off"
    )


async def test_battery2_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The second battery block (library 0.3.0) mirrors the first one."""
    # battery2_power = +111 (discharging) and battery2_current = +2.1.
    dongle.poll_result = make_poll(
        overrides={"battery2_power": 111, "battery2_current": 2.1}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "battery2_soc").state == "82"
    assert state_for(hass, "sensor", "battery2_cycles").state == "45"
    assert float(state_for(hass, "sensor", "battery2_power").state) == 111.0

    # The computed split works off battery2_power, not battery_power.
    assert float(state_for(hass, "sensor", "battery2_discharge_power").state) == 111.0
    assert float(state_for(hass, "sensor", "battery2_charge_power").state) == 0.0
    assert state_for(hass, "sensor", "battery2_flow").state == "discharging"
    assert (
        float(state_for(hass, "sensor", "battery2_discharge_current").state)
        == pytest.approx(2.1)
    )

    # Enum maps are shared with battery 1.
    assert state_for(hass, "sensor", "battery2_status").state == "charging"
    assert state_for(hass, "sensor", "battery2_bms_status").state == "ok"

    # The forbid flags of battery 2 get their own binary sensors.
    assert state_for(hass, "binary_sensor", "battery2_charge_forbidden").state == "off"
    assert (
        state_for(hass, "binary_sensor", "battery2_discharge_forbidden").state == "off"
    )

    # Battery 2's signed current carries the same sign-convention attribute.
    power = state_for(hass, "sensor", "battery2_power")
    assert "discharge" in power.attributes["sign_convention"]


async def test_battery2_forbidden_binary_sensors(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Battery 2's forbid flags flip their own binary sensors."""
    dongle.poll_result = make_poll(
        overrides={"battery2_forbid_charge": 1, "battery2_forbid_discharge": 1}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "binary_sensor", "battery2_charge_forbidden").state == "on"
    assert (
        state_for(hass, "binary_sensor", "battery2_discharge_forbidden").state == "on"
    )


async def test_meter2_and_self_test_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """The second meter and the self-test registers (library 0.3.0)."""
    dongle.poll_result = make_poll(
        overrides={"meter_testing_result": 0xAA55, "meter2_r_current": 14.2}
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    # meter2 total P: -190 W (importing) like meter 1's sign convention.
    assert float(state_for(hass, "sensor", "meter2_total_p").state) == -190.0
    total_p = state_for(hass, "sensor", "meter2_total_p")
    assert "import" in total_p.attributes["sign_convention"]

    # meter2_r_current gets the same 0.1 -> 0.01 scale correction as meter 1:
    # encoded at the protocol's 0.1 scale, then multiplied by factor 0.1.
    current = state_for(hass, "sensor", "meter2_r_current")
    assert float(current.state) == pytest.approx(1.42)
    assert "scale corrected" in current.attributes["note"]

    # The meter 2 energy totals are lifetime counters.
    assert state_for(hass, "sensor", "meter2_energy_to_grid_total").state == "55.4"

    # The self-test result enum maps its raw codes to translated slugs:
    # 0xAA55 is the "still testing" sentinel.
    assert state_for(hass, "sensor", "meter_testing_result").state == "testing"

    # The progress sensor exists but is a disabled-by-default diagnostic.
    from homeassistant.helpers import entity_registry as er

    reg_entry = er.async_get(hass).async_get_entity_id(
        "sensor", "sitsolar", "INV1234567_meter_testing_process"
    )
    assert reg_entry is not None
    entry = er.async_get(hass).async_get(reg_entry)
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    # S/T-phase entities of meter 2 follow the same three-phase gate.
    assert state_for(hass, "sensor", "meter2_s_voltage") is None


async def test_meter_testing_result_enum(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A non-sentinel self-test code maps to its own slug."""
    dongle.poll_result = make_poll(overrides={"meter_testing_result": 9})
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "meter_testing_result").state == "ct_not_connected"


async def test_unreadable_battery2_and_meter2_blocks_hide_entities(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """Units without a second battery/meter refuse those ranges: no entities."""
    dongle.poll_result = make_poll(
        unreadable_sections=("battery2_parameters", "meter2_parameters")
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "battery2_soc") is None
    assert state_for(hass, "sensor", "battery2_power") is None
    assert state_for(hass, "binary_sensor", "battery2_charge_forbidden") is None
    assert state_for(hass, "sensor", "meter2_total_p") is None
    assert state_for(hass, "sensor", "meter2_r_voltage") is None
    # The first battery and meter keep working.
    assert state_for(hass, "sensor", "battery_soc") is not None
    assert state_for(hass, "sensor", "meter_total_p") is not None


async def test_device_info_populated(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """DeviceInfo carries manufacturer, model, serial and firmware versions."""
    from homeassistant.helpers import device_registry as dr

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        ("sitsolar", "INV1234567"), mock_entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "SiNeng"
    assert device.model == "3K-HS"
    assert device.serial_number == "INV1234567"
    # sw_version aggregates DSP1/DSP2/ARM versions.
    assert device.sw_version


async def test_counter_zero_dip_is_held(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A single bogus low reading of a total_increasing counter never shows.

    The recorder treats a dip below 90 % of the previous value as a meter
    reset and then counts the whole next reading as fresh consumption, so one
    zero (the dongle returning zeros while it re-establishes the inverter
    session after a reconnect) would put the entire counter into the Energy
    dashboard's consumption sum.
    """
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_entry.runtime_data

    counter = state_for(hass, "sensor", "energy_from_pv_total")
    assert counter.state == "5123.4"

    # One poll reads 0 ...
    dongle.client.poll_result = make_poll(overrides={"energy_from_pv_total": 0})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    held = state_for(hass, "sensor", "energy_from_pv_total")
    assert held.state == "5123.4"  # the dip is held back, not published
    assert "assumed_state" not in held.attributes  # the poll itself succeeded

    # ... and the next real reading continues as if nothing had happened.
    dongle.client.poll_result = make_poll(overrides={"energy_from_pv_total": 5123.5})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert state_for(hass, "sensor", "energy_from_pv_total").state == "5123.5"


async def test_persistent_counter_drop_is_accepted_as_reset(
    hass: HomeAssistant, dongle: Any, mock_entry: Any
) -> None:
    """A drop that survives COUNTER_DROP_CONFIRM_POLLS polls is a real reset.

    This keeps the legitimate resets working: the midnight rollover of the
    daily counters, a replaced lifetime counter.
    """
    from custom_components.sitsolar.const import COUNTER_DROP_CONFIRM_POLLS

    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_entry.runtime_data

    today = state_for(hass, "sensor", "energy_today_dc")
    assert today.state == "24.3"

    # The daily counter rolls over at midnight: 24.3 -> 0.0, and it stays 0.
    dongle.client.poll_result = make_poll(overrides={"energy_today_dc": 0})
    for _ in range(COUNTER_DROP_CONFIRM_POLLS - 1):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert state_for(hass, "sensor", "energy_today_dc").state == "24.3"

    # The last consecutive low poll lets the reset through.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(state_for(hass, "sensor", "energy_today_dc").state) == 0.0

    # Counting resumes from the new base.
    dongle.client.poll_result = make_poll(overrides={"energy_today_dc": 0.1})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(state_for(hass, "sensor", "energy_today_dc").state) == 0.1
