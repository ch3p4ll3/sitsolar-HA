"""Base entity for the Sit Solar integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SitSolarCoordinator


class SitSolarEntity(CoordinatorEntity[SitSolarCoordinator]):
    """Common behaviour: one device, translated names, hashed unique ids."""

    _attr_has_entity_name = True
    _attr_device_info: DeviceInfo | None = None

    def __init__(
        self,
        coordinator: SitSolarCoordinator,
        description_key: str,
    ) -> None:
        """Initialise from the coordinator; *description_key* is unique per entry.

        The unique id is ``<inverter serial or sha256 hash>_<key>``: the
        dongle serial (the password) never appears in plain text.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entity_prefix}_{description_key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator failed, or this key is unreadable."""
        return super().available and self._key_readable()

    def _key_readable(self) -> bool:
        """Override in subclasses that map to a single register key."""
        return True
