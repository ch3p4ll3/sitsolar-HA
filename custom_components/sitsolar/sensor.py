"""Sensor platform for the Sit Solar integration.

Every sensor maps to one key of the library's ``poll()`` result (see
``sitsolar_dongle.registers``) or is computed from several of them.  Only
descriptions whose source keys were decoded in the first successful poll are
created, so a unit without an EPS, without a meter, or a single-phase unit
simply never gets those entities.

Hardware quirks handled here (see docs/ENTITIES.md for the rationale):

* ``apparent_power`` (31039) tracks active power on the tested firmware, so
  the real apparent power is computed as sqrt(P^2+Q^2) and the raw register
  is kept as a disabled diagnostic entity.
* ``power_factor`` (31022) is a *setpoint*, not a measurement; the real
  power factor is computed as P/sqrt(P^2+Q^2) and the raw register is kept
  as a disabled diagnostic entity.
* ``meter_r_current`` (33002) is scaled 0.1 by the protocol document but
  current/voltage/power ratios on real hardware indicate 0.01; the value is
  corrected here (factor 0.1) with the raw scale kept in the attribute.  The
  second meter's currents (33102/33107/33112, library 0.3.0) carry the same
  doubt and get the same correction.
* A ``total_increasing`` counter that reads *below* its last published value
  holds that value for a few polls before the drop is let through (see
  ``SitSolarSensor._resolve_counter``): the recorder would otherwise treat a
  single bogus low reading - the dongle returning zeros while it
  re-establishes the inverter session after a reconnect - as a meter reset
  and add the whole counter to the Energy dashboard's consumption sum.

Library 0.3.0 (protocol v0.0.7) added the second battery block
(``battery2_*``), the optional second meter (``meter2_*``) and the meter
self-test registers (``meter_testing_*``); they are exposed here with the
same names, units and corrections as their first-battery/first-meter
counterparts.  Units without a second battery or meter refuse those ranges,
so the entities are simply not created there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import SitSolarConfigEntry
from .const import COUNTER_DROP_CONFIRM_POLLS, LIFETIME_COUNTER_KEYS
from .coordinator import (
    SitSolarCoordinator,
    SitSolarData,
    active_alarms,
    battery_error_words,
    dsp_error_words,
    readable,
)
from .entity import SitSolarEntity

# ---------------------------------------------------------------------------
# helpers used by the computed descriptions
# ---------------------------------------------------------------------------


def _value(data: SitSolarData, key: str) -> float | None:
    """Decoded numeric value of *key*, or None when unreadable."""
    reading = data.poll.get(key)
    if not isinstance(reading, dict) or "value" not in reading:
        return None
    return cast(float, reading["value"])


def _battery_flow(data: SitSolarData, key: str = "battery_power") -> str | None:
    """Charge/discharge/idle enum from the sign of battery power."""
    power = _value(data, key)
    if power is None:
        return None
    return "discharging" if power > 0 else "charging" if power < 0 else "idle"


def _charge_power(data: SitSolarData, key: str = "battery_power") -> float | None:
    power = _value(data, key)
    return None if power is None else max(0.0, -power)


def _discharge_power(data: SitSolarData, key: str = "battery_power") -> float | None:
    power = _value(data, key)
    return None if power is None else max(0.0, power)


def _charge_current(data: SitSolarData, key: str = "battery_current") -> float | None:
    current = _value(data, key)
    return None if current is None else max(0.0, -current)


def _discharge_current(
    data: SitSolarData, key: str = "battery_current"
) -> float | None:
    current = _value(data, key)
    return None if current is None else max(0.0, current)


def _apparent_power(data: SitSolarData) -> float | None:
    """sqrt(P^2 + Q^2): the register-sourced apparent power is unusable."""
    p = _value(data, "inverter_p")
    q = _value(data, "inverter_q")
    if p is None or q is None:
        return None
    return sqrt(p * p + q * q)


def _real_power_factor(data: SitSolarData) -> float | None:
    """P / sqrt(P^2 + Q^2); the register value is a setpoint, not a reading."""
    p = _value(data, "inverter_p")
    q = _value(data, "inverter_q")
    if p is None or q is None:
        return None
    apparent = sqrt(p * p + q * q)
    if apparent <= 0:
        return None
    return p / apparent


def _grid_exchange_power(data: SitSolarData) -> float | None:
    """Net grid exchange, import positive / export negative."""
    meter = _value(data, "meter_total_p")
    return None if meter is None else -meter


def _home_consumption(data: SitSolarData) -> float | None:
    """Estimated instantaneous home consumption in W.

    PV (DC) + battery discharge - battery charge - export + import, which
    simplifies to ``total_pv_power + battery_power - meter_total_p`` because
    battery_power is signed (positive = discharge) and meter_total_p is
    export minus import.  This is DC-side arithmetic: inverter conversion
    losses are not accounted for, so treat it as an estimate.
    """
    pv = _value(data, "total_pv_power")
    battery = _value(data, "battery_power")
    meter = _value(data, "meter_total_p")
    if pv is None or battery is None or meter is None:
        return None
    return pv * 1000.0 + battery - meter


def _dongle_clock(data: SitSolarData) -> datetime | None:
    """Dongle clock reported during the handshake (there is no clock register)."""
    if not data.dongle_time:
        return None
    return dt_util.utc_from_timestamp(data.dongle_time)


# ---------------------------------------------------------------------------
# description dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SitSolarSensorDescription(SensorEntityDescription):
    """Sensor description bound to the library's poll() result."""

    library_key: str | None = None
    """Poll key to read (mutually exclusive with ``compute``)."""

    compute: Callable[[SitSolarData], StateType | datetime] | None = None
    """Derive the value from several poll keys instead of one."""

    requires: tuple[str, ...] = ()
    """Poll keys that must all be readable for this entity to be created."""

    three_phase: bool = False
    """Only create on units whose device_type says three phase."""

    factor: float = 1.0
    """Multiplier applied to the decoded value (kW->W, quirk corrections)."""

    zero_is_unavailable: bool = False
    """Report None while a lifetime counter still reads 0 (never counted)."""

    enum_map: dict[int, str] | None = None
    """Map a raw register code to an enum option slug."""


# enum option slugs (translated via strings.json)
OPERATION_STATUS = {
    0: "waiting",
    1: "self_check",
    2: "normal",
    3: "flash",
    4: "error",
    5: "debug",
    7: "off_grid",
}
BATTERY_STATUS = {0: "sleep", 1: "charging", 2: "discharging", 3: "idle"}
BMS_STATUS = {0: "fail", 1: "ok", 2: "not_chosen"}
# v0.0.7 (change D006) reduced this enum from five values to two; codes 2-4
# only appear on firmware older than the protocol the library transcribes.
MPPT_INPUT_TYPE = {0: "independent_input", 1: "same_panels_parallel"}
# v0.0.7 (change D007): result of the meter self test, 33049.  0xAA55 is the
# "still testing" sentinel and is the only non-sequential code.
METER_TEST_RESULT = {
    0xAA55: "testing",
    1: "meter_comms_fail",
    2: "battery_comms_fail",
    3: "meter_ln_reversed",
    4: "meter_check_successful",
    5: "meter_disabled",
    6: "load_fluctuation_too_large",
    7: "inverter_not_working",
    8: "other_fail",
    9: "ct_not_connected",
    16: "current_lower_than_required",
    17: "incorrect_meter_wiring",
    18: "meter_inverter_interaction_failed",
    19: "meter_self_test_failed",
}

SENSOR_DESCRIPTIONS: tuple[SitSolarSensorDescription, ...] = (
    # -- operation --------------------------------------------------------
    SitSolarSensorDescription(
        key="operation_status",
        translation_key="operation_status",
        library_key="operation_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*OPERATION_STATUS.values(), "unknown"],
        enum_map=OPERATION_STATUS,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="mppt_input_type",
        translation_key="mppt_input_type",
        library_key="mppt_input_type",
        device_class=SensorDeviceClass.ENUM,
        options=[*MPPT_INPUT_TYPE.values(), "unknown"],
        enum_map=MPPT_INPUT_TYPE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="mppt_string_number",
        translation_key="mppt_string_number",
        library_key="mppt_string_number",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="pv_string_number",
        translation_key="pv_string_number",
        library_key="pv_string_number",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="auto_start_remaining",
        translation_key="auto_start_remaining",
        library_key="auto_start_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # -- alarms -----------------------------------------------------------
    SitSolarSensorDescription(
        key="alarm_status",
        translation_key="alarm_status",
        compute=lambda data: _alarm_status(data),
        requires=("dsp_warning1", "dsp_warning5"),
    ),
    # -- temperatures -----------------------------------------------------
    SitSolarSensorDescription(
        key="cabin_temperature",
        translation_key="cabin_temperature",
        library_key="cabin_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="heat_sink_temperature",
        translation_key="heat_sink_temperature",
        library_key="heat_sink_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="buckboost_temperature",
        translation_key="buckboost_temperature",
        library_key="buckboost_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="boost_module1_temperature",
        translation_key="boost_module1_temperature",
        library_key="boost_module1_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="dc_ac_module_temperature",
        translation_key="dc_ac_module_temperature",
        library_key="dc_ac_module_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # -- device info (diagnostics) ----------------------------------------
    SitSolarSensorDescription(
        key="rated_power",
        translation_key="rated_power",
        library_key="rated_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="dsp1_version",
        translation_key="dsp1_version",
        library_key="dsp1_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="dsp2_version",
        translation_key="dsp2_version",
        library_key="dsp2_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="arm_software_version",
        translation_key="arm_software_version",
        library_key="arm_software_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="bms_software_version",
        translation_key="bms_software_version",
        library_key="bms_software_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="dongle_clock",
        translation_key="dongle_clock",
        compute=_dongle_clock,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # -- DC side ----------------------------------------------------------
    SitSolarSensorDescription(
        key="total_pv_power",
        translation_key="total_pv_power",
        library_key="total_pv_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        factor=1000.0,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="pv1_voltage",
        translation_key="pv1_voltage",
        library_key="pv1_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="pv1_current",
        translation_key="pv1_current",
        library_key="pv1_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="pv1_power",
        translation_key="pv1_power",
        library_key="pv1_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        factor=1000.0,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="pv1_energy_today",
        translation_key="pv1_energy_today",
        library_key="pv1_energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="pv2_voltage",
        translation_key="pv2_voltage",
        library_key="pv2_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="pv2_current",
        translation_key="pv2_current",
        library_key="pv2_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="pv2_power",
        translation_key="pv2_power",
        library_key="pv2_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        factor=1000.0,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="pv2_energy_today",
        translation_key="pv2_energy_today",
        library_key="pv2_energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
    ),
    # -- AC / grid --------------------------------------------------------
    SitSolarSensorDescription(
        key="inverter_p",
        translation_key="inverter_p",
        library_key="inverter_p",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="inverter_q",
        translation_key="inverter_q",
        library_key="inverter_q",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="apparent_power_calc",
        translation_key="apparent_power_calc",
        compute=_apparent_power,
        requires=("inverter_p", "inverter_q"),
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="apparent_power",
        translation_key="apparent_power",
        library_key="apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="power_factor_calc",
        translation_key="power_factor_calc",
        compute=_real_power_factor,
        requires=("inverter_p", "inverter_q"),
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="power_factor",
        translation_key="power_factor",
        library_key="power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        library_key="grid_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="phase_r_voltage",
        translation_key="phase_r_voltage",
        library_key="phase_r_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="phase_r_current",
        translation_key="phase_r_current",
        library_key="phase_r_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="phase_r_power",
        translation_key="phase_r_power",
        library_key="phase_r_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="phase_s_voltage",
        translation_key="phase_s_voltage",
        library_key="phase_s_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="phase_s_current",
        translation_key="phase_s_current",
        library_key="phase_s_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="phase_s_power",
        translation_key="phase_s_power",
        library_key="phase_s_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="phase_t_voltage",
        translation_key="phase_t_voltage",
        library_key="phase_t_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="phase_t_current",
        translation_key="phase_t_current",
        library_key="phase_t_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="phase_t_power",
        translation_key="phase_t_power",
        library_key="phase_t_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="grid_line_rs",
        translation_key="grid_line_rs",
        library_key="grid_line_rs",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="grid_line_st",
        translation_key="grid_line_st",
        library_key="grid_line_st",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="grid_line_tr",
        translation_key="grid_line_tr",
        library_key="grid_line_tr",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="grid_n_pe_voltage",
        translation_key="grid_n_pe_voltage",
        library_key="grid_n_pe_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    # -- grid exchange (computed) -----------------------------------------
    SitSolarSensorDescription(
        key="grid_exchange_power",
        translation_key="grid_exchange_power",
        compute=_grid_exchange_power,
        requires=("meter_total_p",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="home_consumption",
        translation_key="home_consumption",
        compute=_home_consumption,
        requires=("total_pv_power", "battery_power", "meter_total_p"),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter_total_p",
        translation_key="meter_total_p",
        library_key="meter_total_p",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter_total_q",
        translation_key="meter_total_q",
        library_key="meter_total_q",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter_power_factor",
        translation_key="meter_power_factor",
        library_key="meter_power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="energy_to_grid_total",
        translation_key="energy_to_grid_total",
        library_key="energy_to_grid_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="energy_from_grid_total",
        translation_key="energy_from_grid_total",
        library_key="energy_from_grid_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    # -- meter phases -----------------------------------------------------
    SitSolarSensorDescription(
        key="meter_r_voltage",
        translation_key="meter_r_voltage",
        library_key="meter_r_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter_r_current",
        translation_key="meter_r_current",
        library_key="meter_r_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter_r_frequency",
        translation_key="meter_r_frequency",
        library_key="meter_r_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter_r_power",
        translation_key="meter_r_power",
        library_key="meter_r_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter_s_voltage",
        translation_key="meter_s_voltage",
        library_key="meter_s_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter_s_current",
        translation_key="meter_s_current",
        library_key="meter_s_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Same unverified 0.1 scale as the R phase (33002); corrected to 0.01.
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter_s_frequency",
        translation_key="meter_s_frequency",
        library_key="meter_s_frequency",
        three_phase=True,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter_s_power",
        translation_key="meter_s_power",
        library_key="meter_s_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter_t_voltage",
        translation_key="meter_t_voltage",
        library_key="meter_t_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter_t_current",
        translation_key="meter_t_current",
        library_key="meter_t_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Same unverified 0.1 scale as the R phase (33002); corrected to 0.01.
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter_t_frequency",
        translation_key="meter_t_frequency",
        library_key="meter_t_frequency",
        three_phase=True,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter_t_power",
        translation_key="meter_t_power",
        library_key="meter_t_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    # -- meter self test (library 0.3.0, protocol v0.0.7 change D007) ------
    SitSolarSensorDescription(
        key="meter_testing_process",
        translation_key="meter_testing_process",
        library_key="meter_testing_process",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="meter_testing_result",
        translation_key="meter_testing_result",
        library_key="meter_testing_result",
        device_class=SensorDeviceClass.ENUM,
        options=[*METER_TEST_RESULT.values(), "unknown"],
        enum_map=METER_TEST_RESULT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # -- meter 2 (library 0.3.0, protocol v0.0.7 change D009) --------------
    # A verbatim mirror of the meter block at +100, for the optional second
    # meter; units without it refuse the range, so no entities are created.
    SitSolarSensorDescription(
        key="meter2_total_p",
        translation_key="meter2_total_p",
        library_key="meter2_total_p",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter2_total_q",
        translation_key="meter2_total_q",
        library_key="meter2_total_q",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter2_power_factor",
        translation_key="meter2_power_factor",
        library_key="meter2_power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="meter2_energy_to_grid_total",
        translation_key="meter2_energy_to_grid_total",
        library_key="meter2_energy_to_grid_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="meter2_energy_from_grid_total",
        translation_key="meter2_energy_from_grid_total",
        library_key="meter2_energy_from_grid_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="meter2_r_voltage",
        translation_key="meter2_r_voltage",
        library_key="meter2_r_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter2_r_current",
        translation_key="meter2_r_current",
        library_key="meter2_r_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Same unverified 0.1 scale as the R phase (33002); corrected to 0.01.
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter2_r_frequency",
        translation_key="meter2_r_frequency",
        library_key="meter2_r_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter2_r_power",
        translation_key="meter2_r_power",
        library_key="meter2_r_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter2_s_voltage",
        translation_key="meter2_s_voltage",
        library_key="meter2_s_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter2_s_current",
        translation_key="meter2_s_current",
        library_key="meter2_s_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Same unverified 0.1 scale as the R phase (33002); corrected to 0.01.
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter2_s_frequency",
        translation_key="meter2_s_frequency",
        library_key="meter2_s_frequency",
        three_phase=True,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter2_s_power",
        translation_key="meter2_s_power",
        library_key="meter2_s_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="meter2_t_voltage",
        translation_key="meter2_t_voltage",
        library_key="meter2_t_voltage",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="meter2_t_current",
        translation_key="meter2_t_current",
        library_key="meter2_t_current",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Same unverified 0.1 scale as the R phase (33002); corrected to 0.01.
        factor=0.1,
        suggested_display_precision=3,
    ),
    SitSolarSensorDescription(
        key="meter2_t_frequency",
        translation_key="meter2_t_frequency",
        library_key="meter2_t_frequency",
        three_phase=True,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="meter2_t_power",
        translation_key="meter2_t_power",
        library_key="meter2_t_power",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    # -- battery ----------------------------------------------------------
    SitSolarSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        library_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery_soh",
        translation_key="battery_soh",
        library_key="battery_soh",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        library_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        library_key="battery_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        library_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery_charge_power",
        translation_key="battery_charge_power",
        compute=_charge_power,
        requires=("battery_power",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery_discharge_power",
        translation_key="battery_discharge_power",
        compute=_discharge_power,
        requires=("battery_power",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery_charge_current",
        translation_key="battery_charge_current",
        compute=_charge_current,
        requires=("battery_current",),
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery_discharge_current",
        translation_key="battery_discharge_current",
        compute=_discharge_current,
        requires=("battery_current",),
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery_flow",
        translation_key="battery_flow",
        compute=_battery_flow,
        requires=("battery_power",),
        device_class=SensorDeviceClass.ENUM,
        options=["charging", "discharging", "idle"],
    ),
    SitSolarSensorDescription(
        key="battery_status",
        translation_key="battery_status",
        library_key="battery_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*BATTERY_STATUS.values(), "unknown"],
        enum_map=BATTERY_STATUS,
    ),
    SitSolarSensorDescription(
        key="bms_status",
        translation_key="bms_status",
        library_key="bms_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*BMS_STATUS.values(), "unknown"],
        enum_map=BMS_STATUS,
    ),
    SitSolarSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        library_key="battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="battery_cycles",
        translation_key="battery_cycles",
        library_key="battery_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="max_charge_voltage",
        translation_key="max_charge_voltage",
        library_key="max_charge_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="max_charge_current",
        translation_key="max_charge_current",
        library_key="max_charge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="min_discharge_voltage",
        translation_key="min_discharge_voltage",
        library_key="min_discharge_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="max_discharge_current",
        translation_key="max_discharge_current",
        library_key="max_discharge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="forced_mode_request",
        translation_key="forced_mode_request",
        library_key="forced_mode_request",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="highest_cell_voltage",
        translation_key="highest_cell_voltage",
        library_key="highest_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="lowest_cell_voltage",
        translation_key="lowest_cell_voltage",
        library_key="lowest_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="highest_cell_temperature",
        translation_key="highest_cell_temperature",
        library_key="highest_cell_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="lowest_cell_temperature",
        translation_key="lowest_cell_temperature",
        library_key="lowest_cell_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # -- battery 2 (library 0.3.0, protocol v0.0.7 change D005) ------------
    # A verbatim mirror of the battery block at +200, only present on
    # inverters with two BMS interfaces; single-battery units refuse the
    # range (or return zeros), so these entities simply never get created.
    SitSolarSensorDescription(
        key="battery2_soc",
        translation_key="battery2_soc",
        library_key="battery2_soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery2_soh",
        translation_key="battery2_soh",
        library_key="battery2_soh",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery2_voltage",
        translation_key="battery2_voltage",
        library_key="battery2_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="battery2_current",
        translation_key="battery2_current",
        library_key="battery2_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery2_power",
        translation_key="battery2_power",
        library_key="battery2_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery2_charge_power",
        translation_key="battery2_charge_power",
        compute=lambda data: _charge_power(data, "battery2_power"),
        requires=("battery2_power",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery2_discharge_power",
        translation_key="battery2_discharge_power",
        compute=lambda data: _discharge_power(data, "battery2_power"),
        requires=("battery2_power",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="battery2_charge_current",
        translation_key="battery2_charge_current",
        compute=lambda data: _charge_current(data, "battery2_current"),
        requires=("battery2_current",),
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery2_discharge_current",
        translation_key="battery2_discharge_current",
        compute=lambda data: _discharge_current(data, "battery2_current"),
        requires=("battery2_current",),
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="battery2_flow",
        translation_key="battery2_flow",
        compute=lambda data: _battery_flow(data, "battery2_power"),
        requires=("battery2_power",),
        device_class=SensorDeviceClass.ENUM,
        options=["charging", "discharging", "idle"],
    ),
    SitSolarSensorDescription(
        key="battery2_status",
        translation_key="battery2_status",
        library_key="battery2_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*BATTERY_STATUS.values(), "unknown"],
        enum_map=BATTERY_STATUS,
    ),
    SitSolarSensorDescription(
        key="battery2_bms_status",
        translation_key="battery2_bms_status",
        library_key="battery2_bms_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*BMS_STATUS.values(), "unknown"],
        enum_map=BMS_STATUS,
    ),
    SitSolarSensorDescription(
        key="battery2_temperature",
        translation_key="battery2_temperature",
        library_key="battery2_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="battery2_cycles",
        translation_key="battery2_cycles",
        library_key="battery2_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="battery2_max_charge_voltage",
        translation_key="battery2_max_charge_voltage",
        library_key="battery2_max_charge_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="battery2_max_charge_current",
        translation_key="battery2_max_charge_current",
        library_key="battery2_max_charge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="battery2_min_discharge_voltage",
        translation_key="battery2_min_discharge_voltage",
        library_key="battery2_min_discharge_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="battery2_max_discharge_current",
        translation_key="battery2_max_discharge_current",
        library_key="battery2_max_discharge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SitSolarSensorDescription(
        key="battery2_forced_mode_request",
        translation_key="battery2_forced_mode_request",
        library_key="battery2_forced_mode_request",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="battery2_highest_cell_voltage",
        translation_key="battery2_highest_cell_voltage",
        library_key="battery2_highest_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="battery2_lowest_cell_voltage",
        translation_key="battery2_lowest_cell_voltage",
        library_key="battery2_lowest_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="battery2_highest_cell_temperature",
        translation_key="battery2_highest_cell_temperature",
        library_key="battery2_highest_cell_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="battery2_lowest_cell_temperature",
        translation_key="battery2_lowest_cell_temperature",
        library_key="battery2_lowest_cell_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # -- EPS --------------------------------------------------------------
    SitSolarSensorDescription(
        key="eps_voltage_r",
        translation_key="eps_voltage_r",
        library_key="eps_voltage_r",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_current_r",
        translation_key="eps_current_r",
        library_key="eps_current_r",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="eps_p_r",
        translation_key="eps_p_r",
        library_key="eps_p_r",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_s",
        translation_key="eps_voltage_s",
        library_key="eps_voltage_s",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_current_s",
        translation_key="eps_current_s",
        library_key="eps_current_s",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="eps_p_s",
        translation_key="eps_p_s",
        library_key="eps_p_s",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_t",
        translation_key="eps_voltage_t",
        library_key="eps_voltage_t",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_current_t",
        translation_key="eps_current_t",
        library_key="eps_current_t",
        three_phase=True,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    SitSolarSensorDescription(
        key="eps_p_t",
        translation_key="eps_p_t",
        library_key="eps_p_t",
        three_phase=True,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="eps_total_p",
        translation_key="eps_total_p",
        library_key="eps_total_p",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="eps_total_q",
        translation_key="eps_total_q",
        library_key="eps_total_q",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        suggested_display_precision=0,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_rs",
        translation_key="eps_voltage_rs",
        library_key="eps_voltage_rs",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_st",
        translation_key="eps_voltage_st",
        library_key="eps_voltage_st",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_tr",
        translation_key="eps_voltage_tr",
        library_key="eps_voltage_tr",
        three_phase=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    SitSolarSensorDescription(
        key="eps_voltage_n_pe",
        translation_key="eps_voltage_n_pe",
        library_key="eps_voltage_n_pe",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    # -- generation info (lifetime and daily counters) ---------------------
    *(
        SitSolarSensorDescription(
            key=key,
            translation_key=key,
            library_key=key,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=1,
            zero_is_unavailable=key in LIFETIME_COUNTER_KEYS,
        )
        for key in (
            "energy_from_inverter_total",
            "energy_to_inverter_total",
            "energy_from_pv_total",
            "energy_to_battery_total",
            "energy_from_battery_total",
            "energy_grid_to_battery_total",
            "energy_to_eps_total",
            "energy_today_dc",
            "energy_today_battery_charge",
            "energy_today_battery_discharge",
            "energy_today_grid_charge",
            "energy_today_sell_to_grid",
            "energy_today_buy_from_grid",
            "energy_today_eps",
            "energy_today_battery_from_pv",
            "energy_today_eps_from_pv",
            "energy_today_pv_sell_to_grid",
            "energy_today_battery_sell_to_grid",
            "energy_today_battery_eps",
            "energy_total_pv_charge_battery",
            "energy_total_eps_from_pv",
            "energy_total_pv_sell_to_grid",
            "energy_total_battery_to_grid",
            "energy_total_battery_to_eps",
        )
    ),
    # hours counters are durations, not energies
    SitSolarSensorDescription(
        key="hours_generation_total",
        translation_key="hours_generation_total",
        library_key="hours_generation_total",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="hours_eps_total",
        translation_key="hours_eps_total",
        library_key="hours_eps_total",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        zero_is_unavailable=True,
    ),
    SitSolarSensorDescription(
        key="hours_today_generation",
        translation_key="hours_today_generation",
        library_key="hours_today_generation",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
    ),
    # -- integration-side diagnostics --------------------------------------
    SitSolarSensorDescription(
        key="last_successful_update",
        translation_key="last_successful_update",
        compute=lambda data: data.updated_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="poll_latency",
        translation_key="poll_latency",
        compute=lambda data: round(data.latency * 1000.0, 1),
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SitSolarSensorDescription(
        key="unreadable_ranges",
        translation_key="unreadable_ranges",
        compute=lambda data: len(data.unreadable),
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


def _alarm_status(data: SitSolarData) -> str:
    """Human-readable alarm status: 'OK' or the active warning names."""
    alarms = active_alarms(data.poll)
    errors = dsp_error_words(data.poll)
    if not alarms and not errors:
        return "OK"
    parts = [str(alarm["name"]) for alarm in alarms]
    parts.extend(f"{key} = 0x{value:04X}" for key, value in errors.items())
    return "; ".join(parts)


def _description_creatable(
    description: SitSolarSensorDescription, coordinator: SitSolarCoordinator
) -> bool:
    """Whether the described sensor exists on this unit's current poll."""
    if description.three_phase and not coordinator.is_three_phase:
        return False
    data = coordinator.data
    if description.compute is not None or description.requires:
        return all(readable(data, key) for key in description.requires)
    if description.library_key is not None:
        return readable(data, description.library_key)
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SitSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the sensors whose registers answered in the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            (
                SitSolarAlarmSensor(coordinator, description)
                if description.key == "alarm_status"
                else SitSolarSensor(coordinator, description)
            )
            for description in SENSOR_DESCRIPTIONS
            if _description_creatable(description, coordinator)
        ]
    )


class SitSolarSensor(SitSolarEntity, SensorEntity):
    """One sensor backed by a poll key or a computation over several keys."""

    entity_description: SitSolarSensorDescription

    def __init__(
        self,
        coordinator: SitSolarCoordinator,
        description: SitSolarSensorDescription,
    ) -> None:
        """Bind the sensor to its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = description.options
        # Drop protection for total_increasing counters (see _resolve_counter):
        # the poll currently handed to HA, the value handed over, the last
        # accepted value, and how many consecutive polls read below it.
        self._tracked_data: SitSolarData | None = None
        self._published_value: float | int | None = None
        self._held_value: float | int | None = None
        self._drop_polls = 0

    @property
    def native_value(self) -> StateType | datetime | None:
        """Decoded, corrected or computed value of this sensor."""
        description = self.entity_description
        data = self.coordinator.data
        if description.compute is not None:
            return description.compute(data)
        reading = data.poll.get(description.library_key or "")
        if not isinstance(reading, dict) or "value" not in reading:
            return None
        value: Any = reading["value"]
        if description.enum_map is not None:
            return description.enum_map.get(int(value), "unknown")
        if description.factor != 1.0:
            value = value * description.factor
            if isinstance(value, float):
                value = round(value, 3)
        if self._holds_counter_drops:
            return self._counter_value(value)
        return cast(StateType, value)

    @property
    def _holds_counter_drops(self) -> bool:
        """True for raw ``total_increasing`` counters.

        Their long-term statistics break on a single bogus low reading (see
        ``_resolve_counter``); computed entities and enums are exempt.
        """
        description = self.entity_description
        return (
            description.library_key is not None
            and description.state_class is SensorStateClass.TOTAL_INCREASING
        )

    def _counter_value(self, value: float | int) -> float | int:
        """Value to hand to HA for this poll, with drops held back.

        Evaluated once per successful poll: the coordinator hands out a
        fresh ``SitSolarData`` object every time, so object identity tracks
        the poll without relying on the clock.  A failed poll keeps the old
        object, and therefore the old value, so this never advances while
        the connection is down.
        """
        data = self.coordinator.data
        if data is not self._tracked_data:
            self._tracked_data = data
            self._published_value = self._resolve_counter(value)
        return cast(float | int, self._published_value)

    def _resolve_counter(self, value: float | int) -> float | int:
        """Hold a counter that reads below its last published value.

        The recorder treats a dip below 90 % of the previous state of a
        ``total_increasing`` sensor as a meter reset and then counts the
        whole next reading as fresh consumption: one bogus zero (the dongle
        returning zeros while it re-establishes the inverter session after a
        reconnect) would put the entire counter into the Energy dashboard.
        The drop has to survive ``COUNTER_DROP_CONFIRM_POLLS`` consecutive
        polls before it is let through as a real reset - the midnight
        rollover of the ``today`` counters, a replaced counter.  A value
        that recovers in the meantime was a glitch and is simply dropped.
        """
        previous = self._held_value
        if previous is None or value >= previous:
            self._held_value = value
            self._drop_polls = 0
            return value
        self._drop_polls += 1
        if self._drop_polls >= COUNTER_DROP_CONFIRM_POLLS:
            # Persistent: take it as a genuine reset and count from here.
            self._held_value = value
            self._drop_polls = 0
            return value
        return previous

    def _is_zero_locked(self) -> bool:
        """True when a lifetime counter reads 0 and has never been nonzero."""
        description = self.entity_description
        key = description.library_key
        if not description.zero_is_unavailable or key is None:
            return False
        reading = self.coordinator.data.poll.get(key)
        if not isinstance(reading, dict) or "value" not in reading:
            return False
        # A lifetime counter that has never left 0 stays unavailable instead
        # of polluting long-term statistics with a stuck zero.
        return reading["value"] == 0 and key not in self.coordinator.alive_counters

    @property
    def available(self) -> bool:
        """Unavailable while the poll fails or this register is unreadable."""
        if not super().available:
            return False
        if self._is_zero_locked():
            return False
        description = self.entity_description
        if description.compute is not None or description.requires:
            return all(
                readable(self.coordinator.data, key) for key in description.requires
            )
        return readable(self.coordinator.data, description.library_key or "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Sign conventions and quirk notes for the values that need them."""
        key = self.entity_description.key
        attrs: dict[str, Any] = {}
        if key in {
            "battery_current",
            "battery_power",
            "battery2_current",
            "battery2_power",
        }:
            attrs["sign_convention"] = "positive = discharge, negative = charge"
        if key in {"meter_total_p", "meter2_total_p"}:
            attrs["sign_convention"] = "positive = export to grid, negative = import"
        if key == "grid_exchange_power":
            attrs["sign_convention"] = "positive = import, negative = export"
        if key == "home_consumption":
            attrs["note"] = (
                "estimate from DC-side values (PV + battery - grid exchange); "
                "inverter conversion losses are not accounted for"
            )
        if key in {"meter_r_current", "meter2_r_current"}:
            attrs["note"] = (
                "scale corrected from 0.1 to 0.01 (protocol document value "
                "fails a V*I vs P consistency check)"
            )
        if key == "apparent_power":
            attrs["note"] = (
                "raw register value; known to track active power on the "
                "tested firmware - the computed entity is reliable"
            )
        if key == "power_factor":
            attrs["note"] = "raw register value; this is a setpoint, not a measurement"
        return attrs


class SitSolarAlarmSensor(SitSolarSensor):
    """Alarm status sensor carrying the decoded alarm lists as attributes."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Active alarms, raw DSP errors and raw battery errors."""
        data = self.coordinator.data
        return {
            "active_alarms": active_alarms(data.poll),
            "dsp_errors": dsp_error_words(data.poll),
            "battery_errors": battery_error_words(data.poll),
        }
