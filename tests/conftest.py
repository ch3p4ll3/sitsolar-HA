"""Shared fixtures: a fake dongle client and a realistic poll builder.

The poll builder encodes values back into raw 16-bit words and runs them
through the library's own ``decode()``, so the dicts the tests feed into the
coordinator are shaped exactly like a real ``poll()``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sitsolar.const import (
    CONF_DONGLE_SERIAL,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from sitsolar_dongle import DongleInfo
from sitsolar_dongle.decode import decode
from sitsolar_dongle.registers import ASCII, REGISTERS, S32, U32

pytest_plugins = "pytest_homeassistant_custom_component"

TEST_HOST = "192.0.2.1"
TEST_DONGLE_SERIAL = "DGL1234567"
TEST_INVERTER_SERIAL = "INV1234567"
TEST_MODEL = "3K-HS"

REGISTERS_BY_KEY = {reg.key: reg for reg in REGISTERS}


# ---------------------------------------------------------------------------
# raw-word encoding helpers (inverse of decode)
# ---------------------------------------------------------------------------


def ascii_words(text: str, length: int) -> tuple[int, ...]:
    """Encode *text* as NUL-padded big-endian words for an ASCII register."""
    data = text.encode("ascii")[: length * 2].ljust(length * 2, b"\x00")
    return tuple(int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2))


def identity_words(
    serial: str = TEST_INVERTER_SERIAL, model: str = TEST_MODEL
) -> tuple[int, ...]:
    """19 words covering serial_number (30001~30010) then model (30011~30019)."""
    return ascii_words(serial, 10) + ascii_words(model, 9)


def _encode(reg: Any, value: Any) -> tuple[int, ...]:
    """Encode a decoded *value* back into the raw words for *reg*."""
    if reg.dtype == ASCII:
        return ascii_words(str(value), reg.length)
    raw = round(value / reg.scale)
    if reg.dtype in (U32, S32):
        if raw < 0:
            raw += 1 << 32
        return ((raw >> 16) & 0xFFFF, raw & 0xFFFF)
    if raw < 0:
        raw += 1 << 16
    return (raw & 0xFFFF,)


# Sensible non-zero values for a single-phase 3K-HS with battery, EPS and a
# grid meter; every unlisted register decodes to 0 (or "" for ASCII).
DEFAULT_POLL_VALUES: dict[str, Any] = {
    # device_info
    "device_type": 33,  # 0x21 = single phase high voltage
    "serial_number": TEST_INVERTER_SERIAL,
    "model": TEST_MODEL,
    "brand": "Sit Solar",
    "manufacturer": "SiNeng",
    "rated_power": 3000,
    "dsp1_version": "1.0.7",
    "dsp2_version": "1.0.9",
    # operation_data
    "operation_status": 2,  # normal
    "mppt_input_type": 1,
    "mppt_string_number": 1,
    "pv_string_number": 2,
    # error_info: no alarms
    "dsp_warning5": 200,
    # temperatures
    "cabin_temperature": 31.5,
    "heat_sink_temperature": 38.0,
    "buckboost_temperature": 29.0,
    "boost_module1_temperature": 27.5,
    "dc_ac_module_temperature": 33.0,
    # ARM_data
    "arm_software_version": "V2.1.0",
    "bms_software_version": "B1.4.2",
    # DC_side
    "total_pv_power": 3.45,
    "pv1_voltage": 245.0,
    "pv1_current": 7.1,
    "pv1_power": 1.73,
    "pv1_energy_today": 12.4,
    "pv2_voltage": 240.0,
    "pv2_current": 7.0,
    "pv2_power": 1.68,
    "pv2_energy_today": 11.9,
    # AC_side
    "phase_r_voltage": 230.0,
    "phase_r_current": 1.85,
    "grid_frequency": 50.01,
    "phase_r_power": 417,
    "inverter_p": 417,
    "inverter_q": 210,
    "power_factor": 0.85,
    "apparent_power": 450,
    # EPS
    "eps_voltage_r": 230.0,
    "eps_current_r": 1.2,
    "eps_p_r": 276,
    "eps_total_p": 276,
    "eps_total_q": 40,
    "eps_voltage_n_pe": 0.0,
    # battery
    "battery_status": 2,  # discharge
    "bms_status": 1,  # ok
    "battery_voltage": 53.2,
    "battery_current": -3.7,
    "battery_power": -197,
    "battery_temperature": 24.5,
    "battery_soc": 64,
    "battery_soh": 98,
    "forbid_charge": 0,
    "forbid_discharge": 0,
    "battery_cycles": 210,
    "max_charge_voltage": 57.6,
    "max_charge_current": 40.0,
    "min_discharge_voltage": 44.0,
    "max_discharge_current": 40.0,
    "highest_cell_voltage": 3.312,
    "lowest_cell_voltage": 3.298,
    "highest_cell_temperature": 25.0,
    "lowest_cell_temperature": 24.0,
    # battery 2 (library 0.3.0)
    "battery2_status": 1,  # charge
    "battery2_bms_status": 1,  # ok
    "battery2_voltage": 52.8,
    "battery2_current": -2.1,
    "battery2_power": -111,
    "battery2_temperature": 23.0,
    "battery2_soc": 82,
    "battery2_soh": 100,
    "battery2_forbid_charge": 0,
    "battery2_forbid_discharge": 0,
    "battery2_cycles": 45,
    "battery2_max_charge_voltage": 57.6,
    "battery2_max_charge_current": 40.0,
    "battery2_min_discharge_voltage": 44.0,
    "battery2_max_discharge_current": 40.0,
    "battery2_highest_cell_voltage": 3.305,
    "battery2_lowest_cell_voltage": 3.299,
    "battery2_highest_cell_temperature": 23.5,
    "battery2_lowest_cell_temperature": 22.5,
    # meter
    "meter_r_voltage": 231.0,
    "meter_r_current": 1.42,
    "meter_r_frequency": 50.0,
    "meter_r_power": 328,
    "meter_total_p": -215,
    "meter_total_q": 40,
    "meter_power_factor": 0.98,
    "energy_to_grid_total": 1234.5,
    "energy_from_grid_total": 2345.6,
    # meter self test (library 0.3.0)
    "meter_testing_process": 100,
    "meter_testing_result": 4,  # meter check successful
    # meter 2 (library 0.3.0)
    "meter2_r_voltage": 229.5,
    "meter2_r_current": 0.85,
    "meter2_r_frequency": 50.0,
    "meter2_r_power": 190,
    "meter2_total_p": -190,
    "meter2_total_q": 15,
    "meter2_power_factor": 0.99,
    "meter2_energy_to_grid_total": 55.4,
    "meter2_energy_from_grid_total": 77.8,
    # generation_info (totals stay nonzero so they are not treated as stuck)
    "energy_from_inverter_total": 4567.8,
    "energy_to_inverter_total": 12.3,
    "energy_from_pv_total": 5123.4,
    "energy_to_battery_total": 2048.8,
    "energy_from_battery_total": 1804.2,
    "energy_grid_to_battery_total": 101.0,
    "energy_to_eps_total": 88.8,
    "hours_generation_total": 2150.5,
    "hours_eps_total": 12.0,
    "energy_today_dc": 24.3,
    "energy_today_battery_charge": 9.1,
    "energy_today_battery_discharge": 7.4,
    "energy_today_grid_charge": 1.2,
    "energy_today_sell_to_grid": 6.6,
    "energy_today_buy_from_grid": 3.3,
    "energy_today_eps": 0.8,
    "hours_today_generation": 6.5,
    "energy_today_battery_from_pv": 7.9,
    "energy_today_eps_from_pv": 0.5,
    "energy_today_pv_sell_to_grid": 5.1,
    "energy_today_battery_sell_to_grid": 1.5,
    "energy_today_battery_eps": 0.3,
    "energy_total_pv_charge_battery": 950.1,
    "energy_total_eps_from_pv": 40.2,
    "energy_total_pv_sell_to_grid": 812.0,
    "energy_total_battery_to_grid": 150.5,
    "energy_total_battery_to_eps": 20.7,
}


def make_poll(
    overrides: dict[str, Any] | None = None,
    unreadable_sections: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a full poll() result, optionally overriding values or hiding sections."""
    values = dict(DEFAULT_POLL_VALUES)
    if overrides:
        values.update(overrides)

    poll: dict[str, Any] = {}
    for reg in REGISTERS:
        if reg.section in unreadable_sections:
            poll[reg.key] = {
                "address": reg.span,
                "name": reg.name,
                "section": reg.section,
                "error": "unavailable",
            }
            continue
        default: Any = "" if reg.dtype == ASCII else 0
        poll[reg.key] = decode(reg, _encode(reg, values.get(reg.key, default)))

    if unreadable_sections:
        poll["_unreadable_ranges"] = [
            {"range": section, "error": "unavailable"}
            for section in unreadable_sections
        ]
    return poll


# ---------------------------------------------------------------------------
# fake client
# ---------------------------------------------------------------------------


class FakeDongleClient:
    """Stand-in for AsyncDongleClient with scriptable behaviour."""

    def __init__(self, host: str, serial: str, **kwargs: Any) -> None:
        self.host = host
        self.serial = serial
        self.kwargs = kwargs
        self.connected = False
        self.closed = False
        self.info: DongleInfo | None = None
        self.poll_result: dict[str, Any] | None = None
        self.poll_exception: Exception | None = None
        #: One exception raised per poll() call, in order; when exhausted the
        #: persistent ``poll_exception`` (if any) takes over again.
        self.poll_exceptions: list[Exception] = []
        self.connect_exception: Exception | None = None
        self.read_result: tuple[int, ...] | None = None
        self.read_exception: Exception | None = None
        self.connect_calls = 0
        self.poll_calls = 0
        self.read_calls = 0

    async def connect(self) -> DongleInfo:
        self.connect_calls += 1
        if self.connect_exception is not None:
            raise self.connect_exception
        self.connected = True
        self.info = DongleInfo(
            version=1, factory="SiNeng", model="Si-Dongle-L",
            serial=self.serial, time=1700000000,
        )
        return self.info

    async def poll(self, **kwargs: Any) -> dict[str, Any]:
        self.poll_calls += 1
        if self.poll_exceptions:
            raise self.poll_exceptions.pop(0)
        if self.poll_exception is not None:
            raise self.poll_exception
        return self.poll_result if self.poll_result is not None else {}

    async def read(self, address: int, count: int, **kwargs: Any) -> tuple[int, ...]:
        self.read_calls += 1
        if self.read_exception is not None:
            raise self.read_exception
        return self.read_result if self.read_result is not None else identity_words()

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeDongleFactory:
    """Callable factory handed to the integration; records every client."""

    def __init__(self) -> None:
        self.created: list[FakeDongleClient] = []
        self.poll_result: dict[str, Any] | None = make_poll()
        self.poll_exception: Exception | None = None
        self.connect_exception: Exception | None = None
        self.read_result: tuple[int, ...] | None = None
        self.read_exception: Exception | None = None

    @property
    def client(self) -> FakeDongleClient:
        """The most recently created client (the one under test)."""
        return self.created[-1]

    def __call__(self, host: str, serial: str, **kwargs: Any) -> FakeDongleClient:
        client = FakeDongleClient(host, serial, **kwargs)
        client.poll_result = self.poll_result
        client.poll_exception = self.poll_exception
        client.connect_exception = self.connect_exception
        client.read_result = self.read_result
        client.read_exception = self.read_exception
        self.created.append(client)
        return client


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ for every test."""


@pytest.fixture
def dongle(monkeypatch: pytest.MonkeyPatch) -> FakeDongleFactory:
    """Patch the AsyncDongleClient used by both setup and the config flow."""
    factory = FakeDongleFactory()
    monkeypatch.setattr("custom_components.sitsolar.AsyncDongleClient", factory)
    monkeypatch.setattr(
        "custom_components.sitsolar.config_flow.AsyncDongleClient", factory
    )
    return factory


@pytest.fixture
def mock_entry() -> Any:
    """A MockConfigEntry for the inverter under test."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Sit Solar {TEST_MODEL}",
        data={
            CONF_HOST: TEST_HOST,
            CONF_DONGLE_SERIAL: TEST_DONGLE_SERIAL,
            CONF_SCAN_INTERVAL: 30,
        },
        unique_id=TEST_INVERTER_SERIAL,
    )
    return entry


def state_for(
    hass: Any, platform: str, key: str, prefix: str = TEST_INVERTER_SERIAL
) -> Any:
    """Look up a state by the integration's unique_id ``<prefix>_<key>``."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{prefix}_{key}"
    )
    return hass.states.get(entity_id) if entity_id else None
