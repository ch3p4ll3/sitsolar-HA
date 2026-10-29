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
        """Available as long as at least one poll has ever succeeded.

        A failed poll (WiFi blip, the dongle reconnect lockout, a Modbus
        error) must *not* flip the entity to ``unavailable``: the
        coordinator keeps the last good poll, so the entity keeps serving
        it, marked as an assumed state (see :attr:`assumed_state`).

        Unavailable gaps punch holes into the recorder's statistics, and
        helpers that map ``unavailable`` to 0 feed bogus zeros into the
        Energy dashboard.  A zero is worse than a gap: the recorder treats
        a dip below 90 % of the previous state of a ``total_increasing``
        sensor as a meter reset and then counts the whole next reading as
        fresh consumption, i.e. the entire counter lands in one bucket.
        """
        return self.coordinator.data is not None and self._key_readable()

    @property
    def assumed_state(self) -> bool:
        """True while the last poll failed and this value is only the last known one."""
        return not self.coordinator.last_update_success

    def _key_readable(self) -> bool:
        """Override in subclasses that map to a single register key."""
        return True
