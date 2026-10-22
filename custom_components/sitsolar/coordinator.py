"""DataUpdateCoordinator for the Sit Solar inverter integration.

One coordinator (and therefore one ``AsyncDongleClient``) per config entry.
The client's socket stays open between polls: the async client's background
reader answers the dongle's ~37 s heartbeats, which is what keeps the
connection alive, and reconnecting per poll would collide with the dongle's
~75 s reconnect lockout.  After a genuinely dead connection the coordinator
backs off for ``RECONNECT_RETRY_AFTER`` seconds instead of retrying every
tick.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from sitsolar_dongle import (
    REGISTERS,
    AsyncDongleClient,
    DongleError,
    EnvelopeError,
    Register,
)

from .const import (
    CONF_DONGLE_SERIAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LIFETIME_COUNTER_KEYS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    RECONNECT_RETRY_AFTER,
)

_LOGGER = logging.getLogger(__name__)

WARNING_WORD_KEYS = ("dsp_warning1", "dsp_warning2", "dsp_warning3", "dsp_warning4")

_REGISTERS_BY_KEY: dict[str, Register] = {reg.key: reg for reg in REGISTERS}


@dataclass(slots=True)
class SitSolarData:
    """One successful poll cycle plus bookkeeping."""

    poll: Mapping[str, Any]
    unreadable: list[dict[str, Any]]
    latency: float
    updated_at: datetime
    dongle_time: int | None


def readable(data: SitSolarData, key: str) -> bool:
    """True when *key* was decoded successfully in this poll."""
    reading = data.poll.get(key)
    return isinstance(reading, Mapping) and "value" in reading


def active_alarms(poll: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode active alarms from registers 30207-30211.

    30207-30210 are 16-bit bit fields (protocol charts 8-10).  The combined
    warning code for a bit is ``201 + 16*word_index + bit_index``, which
    reproduces chart 11.  Register 30211 ("dsp_warning5") is *not* a bit
    field: it carries a single decimal warning code, where 200 (and 0 on
    firmware that has stored no code yet) means "no warning".  The result is
    deduplicated by code because both sources describe the same code space.
    """
    alarms: list[dict[str, Any]] = []
    seen: set[int] = set()

    for word_index, key in enumerate(WARNING_WORD_KEYS):
        reading = poll.get(key)
        if not isinstance(reading, Mapping) or "value" not in reading:
            continue
        raw = int(reading["value"])
        bits = _REGISTERS_BY_KEY[key].bits
        for bit_index in range(16):
            if not raw & (1 << bit_index):
                continue
            code = 201 + 16 * word_index + bit_index
            if code in seen:
                continue
            seen.add(code)
            if bits and bit_index in bits:
                name: str = bits[bit_index]
            else:
                # 30210 has no chart in the protocol document.
                name = f"warning code {code} (undocumented bit)"
            alarms.append({"code": code, "name": name, "source": key})

    reading = poll.get("dsp_warning5")
    if isinstance(reading, Mapping) and "value" in reading:
        code = int(reading["value"])
        if code not in (0, 200) and code not in seen:
            seen.add(code)
            name = str(reading.get("label") or f"warning code {code}")
            alarms.append({"code": code, "name": name, "source": "dsp_warning5"})

    return alarms


def nonzero_error_words(
    poll: Mapping[str, Any], keys: tuple[str, ...]
) -> dict[str, int]:
    """Nonzero raw values among *keys*; 0 is assumed to mean "no error"."""
    out: dict[str, int] = {}
    for key in keys:
        reading = poll.get(key)
        if not isinstance(reading, Mapping) or "value" not in reading:
            continue
        value = int(reading["value"])
        if value:
            out[key] = value
    return out


def dsp_error_words(poll: Mapping[str, Any]) -> dict[str, int]:
    """Nonzero DSP error words (30200-30206)."""
    return nonzero_error_words(poll, tuple(f"dsp_error{i}" for i in range(1, 8)))


def battery_error_words(poll: Mapping[str, Any]) -> dict[str, int]:
    """Nonzero battery error/warning/protection words (32072-32080)."""
    return nonzero_error_words(
        poll,
        tuple(
            f"battery_{kind}{i}"
            for kind in ("error", "warning", "protection")
            for i in range(1, 4)
        ),
    )


def problem_active(poll: Mapping[str, Any]) -> bool:
    """True when any warning bit, warning code or DSP error word is active."""
    return bool(active_alarms(poll) or dsp_error_words(poll))


class SitSolarCoordinator(DataUpdateCoordinator[SitSolarData]):
    """Poll one Si-Dongle-L dongle through a single long-lived connection."""

    config_entry: ConfigEntry[SitSolarCoordinator]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[SitSolarCoordinator],
        client: AsyncDongleClient,
    ) -> None:
        """Create the coordinator for *entry*'s dongle."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            config_entry=entry,
            update_method=self._async_update_data,
            update_interval=update_interval_for(entry),
        )
        self.client = client
        self._retry_after: float = 0.0
        # Lifetime counters that have read a nonzero value at least once.
        # A counter that never leaves 0 is reported as unavailable rather
        # than as a stuck total_increasing sensor (see README).
        self.alive_counters: set[str] = set()

    # -- identity ------------------------------------------------------
    @property
    def entity_prefix(self) -> str:
        """Prefix for entity unique ids: the inverter serial (never the dongle's).

        Falls back to a hash so the dongle serial never appears in plain text
        in a unique_id, even if the config entry lost its unique_id.
        """
        if unique_id := self.config_entry.unique_id:
            return unique_id
        serial = self.config_entry.data.get(CONF_DONGLE_SERIAL, "")
        return hashlib.sha256(serial.encode()).hexdigest()[:32]

    @property
    def device_info(self) -> DeviceInfo:
        """Device registry info built from the device_info register block."""
        poll = self.data.poll if self.data else {}
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self.entity_prefix)},
            "name": self.config_entry.title,
        }

        manufacturer = _string_value(poll, "manufacturer") or _string_value(
            poll, "brand"
        )
        info["manufacturer"] = manufacturer or "Sit Solar"

        if model := _string_value(poll, "model"):
            info["model"] = model

        if serial := _string_value(poll, "serial_number"):
            info["serial_number"] = serial
        elif self.config_entry.unique_id:
            info["serial_number"] = self.config_entry.unique_id

        versions = [
            f"{label} {value}"
            for key, label in (
                ("dsp1_version", "DSP1"),
                ("dsp2_version", "DSP2"),
                ("arm_software_version", "ARM"),
            )
            if (value := _string_value(poll, key))
        ]
        if versions:
            info["sw_version"] = " / ".join(versions)

        if device_type := _label_value(poll, "device_type"):
            info["hw_version"] = device_type

        return info

    @property
    def is_three_phase(self) -> bool:
        """True only when device_type (30000) positively says three phase.

        The low nibble is 0x13/0x23/0x33 for three-phase units and
        0x11/0x21/0x31 for single-phase ones.  Unknown (unreadable or an
        undocumented code) is treated as single phase so that no S/T-phase
        entities are created on a unit that may not have them.
        """
        poll = self.data.poll if self.data else {}
        reading = poll.get("device_type")
        if not isinstance(reading, Mapping) or "value" not in reading:
            return False
        return int(reading["value"]) & 0x0F == 3

    # -- polling -------------------------------------------------------
    async def _async_update_data(self) -> SitSolarData:
        """Fetch one full poll from the dongle.

        The lockout guard sits *outside* the try on purpose: if it were
        inside, its failure would also run the bookkeeping below and push
        the backoff deadline forward on every tick, which would pin the
        integration unavailable for good.
        """
        if not self.client.connected and (
            remaining := self._retry_after - time.monotonic()
        ) > 0:
            raise UpdateFailed(
                "waiting out the dongle reconnect lockout",
                retry_after=remaining,
            )

        start = time.monotonic()
        try:
            if not self.client.connected:
                await self.client.connect()
            poll = await self.client.poll()
        except EnvelopeError as err:
            # HMAC mismatch: the configured dongle serial does not match.
            # The connection itself is healthy, so no backoff is armed.
            raise ConfigEntryAuthFailed(
                "the dongle rejected the serial number"
            ) from err
        except DongleError as err:
            if not self.client.connected:
                self._retry_after = time.monotonic() + RECONNECT_RETRY_AFTER
                _LOGGER.warning(
                    "connection to the dongle at %s is gone (%s); "
                    "backing off %.0f s for the reconnect lockout",
                    self.config_entry.data.get("host"),
                    err,
                    RECONNECT_RETRY_AFTER,
                )
                raise UpdateFailed(
                    str(err), retry_after=RECONNECT_RETRY_AFTER
                ) from err
            raise UpdateFailed(str(err)) from err
        latency = time.monotonic() - start

        unreadable = list(poll.get("_unreadable_ranges", []))
        for key in LIFETIME_COUNTER_KEYS:
            reading = poll.get(key)
            if isinstance(reading, Mapping) and reading.get("value"):
                self.alive_counters.add(key)

        info = self.client.info
        return SitSolarData(
            poll=poll,
            unreadable=unreadable,
            latency=latency,
            updated_at=dt_util.utcnow(),
            dongle_time=info.time if info is not None else None,
        )


def update_interval_for(entry: ConfigEntry[SitSolarCoordinator]) -> timedelta:
    """Options override config data; both are clamped to the safe range."""
    seconds = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    return timedelta(
        seconds=max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(seconds)))
    )


def _string_value(poll: Mapping[str, Any], key: str) -> str | None:
    """Non-empty decoded value of an ASCII register, if readable."""
    reading = poll.get(key)
    if not isinstance(reading, Mapping):
        return None
    value = reading.get("value")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _label_value(poll: Mapping[str, Any], key: str) -> str | None:
    """Decoded enum label of a register, if readable."""
    reading = poll.get(key)
    if not isinstance(reading, Mapping):
        return None
    label = reading.get("label")
    return str(label) if label else None
