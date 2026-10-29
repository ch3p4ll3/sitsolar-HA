"""Constants for the Sit Solar inverter integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "sitsolar"

# The Si-Dongle-L listens on this TCP port (library default).
DEFAULT_PORT: Final = 7078

# Config flow fields.
CONF_DONGLE_SERIAL: Final = "dongle_serial"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Poll cadence.  30 s is the documented default; the dongle refuses new TCP
# connections for ~75 s after a dropped session, so anything faster than 15 s
# would only ever collide with that lockout.
DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600

# Backoff after a genuinely dead connection, strictly longer than the dongle's
# ~75 s reconnect lockout (see README "Troubleshooting").
RECONNECT_RETRY_AFTER: Final = 90.0

# Timeout for a single connect/handshake or full poll cycle (seconds).
REQUEST_TIMEOUT: Final = 15.0

# Pause before the one-shot retry of a poll that timed out on a connection
# that is still open (seconds).  Short on purpose: the library discards the
# late reply anyway, the pause only exists to ride out a momentary blip.
POLL_RETRY_DELAY: Final = 2.0

# How many consecutive polls a total_increasing counter may read *below* its
# last published value before that drop is accepted as a real reset.  The
# recorder treats a dip below 90 % of the previous value as a meter reset and
# counts the whole next reading as fresh consumption, so a single bogus low
# reading (the dongle returning zeros while it re-establishes the inverter
# session after a reconnect) would put the entire counter into the Energy
# dashboard.  3 polls (~90 s at the default interval) is longer than any
# reconnect artefact observed and short enough that a genuine reset - the
# midnight rollover of the ``today`` counters, a replaced counter - only
# shows up a minute or so late.
COUNTER_DROP_CONFIRM_POLLS: Final = 3

PLATFORMS: Final[list[Platform]] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Diagnostic sensors are created disabled by default.
LIFETIME_COUNTER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "energy_from_inverter_total",
        "energy_to_inverter_total",
        "energy_from_pv_total",
        "energy_to_battery_total",
        "energy_from_battery_total",
        "energy_grid_to_battery_total",
        "energy_to_eps_total",
        "hours_generation_total",
        "hours_eps_total",
        "energy_total_pv_charge_battery",
        "energy_total_eps_from_pv",
        "energy_total_pv_sell_to_grid",
        "energy_total_battery_to_grid",
        "energy_total_battery_to_eps",
        "energy_to_grid_total",
        "energy_from_grid_total",
        "battery_cycles",
        # Second battery / second meter (library 0.3.0).
        "battery2_cycles",
        "meter2_energy_to_grid_total",
        "meter2_energy_from_grid_total",
    }
)
