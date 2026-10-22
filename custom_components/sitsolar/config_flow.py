"""Config, options, reconfigure and reauth flows for the Sit Solar integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import selector
import probatio

from sitsolar_dongle import (
    REGISTERS,
    AsyncDongleClient,
    DongleError,
    EnvelopeError,
    Register,
    decode,
)

from .const import (
    CONF_DONGLE_SERIAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        probatio.Required(CONF_DONGLE_SERIAL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        probatio.Required(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): probatio.All(
            probatio.Coerce(int),
            probatio.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
        ),
    }
)

RECONFIGURE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
    }
)

REAUTH_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_DONGLE_SERIAL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
    }
)

_SERIAL_REGISTER = next(r for r in REGISTERS if r.key == "serial_number")
_MODEL_REGISTER = next(r for r in REGISTERS if r.key == "model")
# serial_number is 30001~30010 and model is 30011~30019: contiguous, so one
# read covers both (and stays well under the 50-register frame cap).
_IDENTITY_ADDRESS = _SERIAL_REGISTER.address
_IDENTITY_COUNT = _MODEL_REGISTER.end - _SERIAL_REGISTER.address + 1


class SitSolarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup, reconfigure and reauth."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate host + dongle serial, then create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            serial = user_input[CONF_DONGLE_SERIAL].strip()
            identity = await self._async_probe(host, serial, errors)
            if identity is not None:
                inverter_serial, model = identity
                # unique_id is the *inverter* serial: stable, non-secret, and
                # it prevents duplicate entries for one physical inverter.
                await self.async_set_unique_id(inverter_serial)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_DONGLE_SERIAL: serial}
                )
                return self.async_create_entry(
                    title=model or f"Sit Solar {host}",
                    data={
                        CONF_HOST: host,
                        CONF_DONGLE_SERIAL: serial,
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the dongle's IP address, keeping the same serial."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            serial = entry.data[CONF_DONGLE_SERIAL]
            identity = await self._async_probe(host, serial, errors)
            if identity is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host}
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, entry.data
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The dongle rejected the stored serial: ask for the right one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a replacement dongle serial."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            serial = user_input[CONF_DONGLE_SERIAL].strip()
            identity = await self._async_probe(entry.data[CONF_HOST], serial, errors)
            if identity is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_DONGLE_SERIAL: serial}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, entry.data
            ),
            errors=errors,
        )

    # -- helpers -------------------------------------------------------
    async def _async_probe(
        self, host: str, serial: str, errors: dict[str, str]
    ) -> tuple[str, str] | None:
        """Handshake and read the inverter serial + model.

        Returns ``(inverter_serial, model)`` on success; on failure fills
        *errors* and returns None.  Never logs the dongle serial, and the
        validation runs entirely in the event loop because the client is
        asyncio-native.
        """
        client = AsyncDongleClient(host, serial, timeout=REQUEST_TIMEOUT)
        try:
            await client.connect()  # BeforeAccredit + Accredit
            words = await client.read(_IDENTITY_ADDRESS, _IDENTITY_COUNT)
        except EnvelopeError:
            errors[CONF_DONGLE_SERIAL] = "invalid_auth"
            return None
        except TimeoutError:
            errors[CONF_HOST] = "cannot_connect"
            return None
        except DongleError as err:
            message = str(err).lower()
            if "cannot connect" in message or "timed out connecting" in message:
                errors[CONF_HOST] = "cannot_connect"
            elif "accredit refused" in message:
                # Wrong serial, or the dongle already serves another client
                # (one connection at a time).
                errors[CONF_DONGLE_SERIAL] = "invalid_auth"
            else:
                _LOGGER.debug("probe failed for %s: %s", host, err)
                errors["base"] = "unknown"
            return None
        finally:
            await client.close()

        serial_value = _decode_ascii(
            _SERIAL_REGISTER, words[: _SERIAL_REGISTER.length]
        )
        offset = _SERIAL_REGISTER.length
        model_value = _decode_ascii(
            _MODEL_REGISTER, words[offset : offset + _MODEL_REGISTER.length]
        )
        if not serial_value:
            errors["base"] = "unknown"
            return None
        return serial_value, model_value

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (update interval)."""
        return SitSolarOptionsFlow()


def _decode_ascii(register: Register, words: tuple[int, ...]) -> str:
    """Decode *words* as an ASCII register; returns "" if malformed."""
    try:
        return str(decode(register, words)["value"])
    except ValueError:
        return ""


class SitSolarOptionsFlow(OptionsFlowWithReload):
    """Update interval; the entry reloads when options change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Form for the poll interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        schema = probatio.Schema(
            {
                probatio.Required(CONF_SCAN_INTERVAL, default=current): probatio.All(
                    probatio.Coerce(int),
                    probatio.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
