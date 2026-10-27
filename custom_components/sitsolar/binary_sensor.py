"""Binary sensor platform for the Sit Solar integration.

The problem binary sensor follows the Home Assistant convention: it is on
whenever any DSP warning bit (30207-30210), a DSP warning code (30211) or a
DSP error word (30200-30206) is active.  Battery error/warning/protection
words (both batteries, 32072-32080 and 32272-32280) are attached as
attributes because their semantics are undocumented.  The charge/discharge
forbidden flags exist once per battery (32069/32070 and, since library
0.3.0, 32269/32270) and get one binary sensor each.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SitSolarConfigEntry
from .coordinator import (
    SitSolarCoordinator,
    SitSolarData,
    battery_error_words,
    problem_active,
    readable,
)
from .entity import SitSolarEntity


@dataclass(frozen=True, kw_only=True)
class SitSolarBinaryDescription(BinarySensorEntityDescription):
    """Binary sensor description bound to the library's poll() result."""

    compute: Callable[[SitSolarData], bool | None] | None = None
    requires: tuple[str, ...] = ()


BINARY_DESCRIPTIONS: tuple[SitSolarBinaryDescription, ...] = (
    SitSolarBinaryDescription(
        key="problem",
        translation_key="problem",
        compute=lambda data: problem_active(data.poll),
        requires=("dsp_warning1", "dsp_warning5"),
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    SitSolarBinaryDescription(
        key="battery_charge_forbidden",
        translation_key="battery_charge_forbidden",
        # 32069: 0 = chargeable, 1 = charge forbidden.
        compute=lambda data: _flag_on(data, "forbid_charge"),
        requires=("forbid_charge",),
    ),
    SitSolarBinaryDescription(
        key="battery_discharge_forbidden",
        translation_key="battery_discharge_forbidden",
        # 32070: 0 = dischargeable, 1 = discharge forbidden.
        compute=lambda data: _flag_on(data, "forbid_discharge"),
        requires=("forbid_discharge",),
    ),
    SitSolarBinaryDescription(
        key="battery2_charge_forbidden",
        translation_key="battery2_charge_forbidden",
        # 32269 (library 0.3.0): second battery's mirror of 32069.
        compute=lambda data: _flag_on(data, "battery2_forbid_charge"),
        requires=("battery2_forbid_charge",),
    ),
    SitSolarBinaryDescription(
        key="battery2_discharge_forbidden",
        translation_key="battery2_discharge_forbidden",
        # 32270 (library 0.3.0): second battery's mirror of 32070.
        compute=lambda data: _flag_on(data, "battery2_forbid_discharge"),
        requires=("battery2_forbid_discharge",),
    ),
)


def _flag_on(data: SitSolarData, key: str) -> bool | None:
    """Interpret an enum {0: off, 1: on} register as a boolean."""
    reading = data.poll.get(key)
    if not isinstance(reading, dict) or "value" not in reading:
        return None
    return int(reading["value"]) == 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SitSolarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the binary sensors whose registers answered in the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SitSolarBinarySensor(coordinator, description)
            for description in BINARY_DESCRIPTIONS
            if all(readable(coordinator.data, key) for key in description.requires)
        ]
    )


class SitSolarBinarySensor(SitSolarEntity, BinarySensorEntity):
    """One binary sensor backed by a flag register or a computation."""

    entity_description: SitSolarBinaryDescription

    def __init__(
        self,
        coordinator: SitSolarCoordinator,
        description: SitSolarBinaryDescription,
    ) -> None:
        """Bind the binary sensor to its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Decoded flag value."""
        compute = self.entity_description.compute
        if compute is None:
            return None
        return compute(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Unavailable while the poll fails or this register is unreadable."""
        if not super().available:
            return False
        return all(
            readable(self.coordinator.data, key)
            for key in self.entity_description.requires
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Battery error words ride along with the problem sensor."""
        if self.entity_description.key != "problem":
            return {}
        return {"battery_errors": battery_error_words(self.coordinator.data.poll)}
