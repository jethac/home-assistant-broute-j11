"""Config and options flow for the B-route Smart Meter (J11) integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector
from serial.tools import list_ports
import voluptuous as vol

from .const import (
    CONF_AUTH_ID,
    CONF_DEVICE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import BrouteConfigEntry, meter_identifier
from .protocol.codec import ProtocolError
from .protocol.commands import (
    CredentialFormatError,
    validate_auth_id,
    validate_password,
)
from .protocol.session import (
    AuthenticationError,
    J11Session,
    MeterNotFoundError,
    SessionConfig,
    SessionError,
    SessionTimeoutError,
)
from .protocol.transport import SerialTransport, TransportError

_LOGGER = logging.getLogger(__name__)


class BrouteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair one adapter and meter, validating input before touching hardware."""

    VERSION = 1

    def __init__(self) -> None:
        """Start a flow that has not paired with a meter yet."""
        self._identifier: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a serial device and the Route-B credentials, then pair."""
        errors: dict[str, str] = {}
        device = ""
        if user_input is not None:
            device = str(user_input[CONF_DEVICE]).strip()
            credentials = self._validate_credentials(user_input, errors)
            if credentials is not None:
                auth_id, password = credentials
                self._async_abort_entries_match({CONF_DEVICE: device})
                error = await self._async_try_pairing(
                    device, SessionConfig(auth_id=auth_id, password=password)
                )
                if error is None:
                    # The meter's identity is only known once it has answered,
                    # so the duplicate check happens after pairing.
                    await self.async_set_unique_id(self._identifier)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Smart meter",
                        data={
                            CONF_DEVICE: device,
                            CONF_AUTH_ID: auth_id,
                            CONF_PASSWORD: password,
                        },
                    )
                errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=await self._async_schema(device),
            errors=errors,
        )

    async def _async_schema(self, device: str) -> vol.Schema:
        """Build the pairing schema, offering the discovered serial devices."""
        ports = await self.hass.async_add_executor_job(list_ports.comports)
        options = [
            selector.SelectOptionDict(
                value=port.device, label=f"{port.description or port.device}"
            )
            for port in ports
        ]
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE, description={"suggested_value": device or None}
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_AUTH_ID): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

    def _validate_credentials(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> tuple[str, str] | None:
        """Check the credential format locally, without contacting hardware."""
        auth_id = ""
        password = ""
        try:
            auth_id = validate_auth_id(str(user_input[CONF_AUTH_ID]))
        except CredentialFormatError:
            errors[CONF_AUTH_ID] = "invalid_auth_id"
        try:
            password = validate_password(str(user_input[CONF_PASSWORD]).strip())
        except CredentialFormatError:
            errors[CONF_PASSWORD] = "invalid_password"
        if errors:
            return None
        return auth_id, password

    async def _async_try_pairing(
        self, device: str, config: SessionConfig
    ) -> str | None:
        """Return an error key, or ``None`` when pairing succeeded.

        On success the paired meter's identifier is kept in ``_identifier``.
        """
        session = J11Session(SerialTransport(device), config)
        try:
            link = await session.async_connect()
        except TransportError:
            return "cannot_connect"
        except AuthenticationError:
            return "invalid_auth"
        except MeterNotFoundError:
            return "no_meter"
        except SessionTimeoutError:
            return "timeout"
        except (SessionError, ProtocolError) as err:
            _LOGGER.debug("Pairing failed: %s", err)
            return "unknown"
        finally:
            await session.async_close()
        self._identifier = meter_identifier(link.mac_address)
        return None

    async def async_step_reauth(
        self, entry_data: Mapping[str, str]
    ) -> ConfigFlowResult:
        """Ask for new credentials after the meter rejected the stored ones."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials against the configured adapter."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = self._validate_credentials(user_input, errors)
            if credentials is not None:
                auth_id, password = credentials
                device = str(entry.data[CONF_DEVICE])
                error = await self._async_try_pairing(
                    device, SessionConfig(auth_id=auth_id, password=password)
                )
                if error is None:
                    if (
                        entry.unique_id is not None
                        and self._identifier != entry.unique_id
                    ):
                        return self.async_abort(reason="wrong_meter")
                    # The unique ID stays put: it identifies the meter, not the
                    # credentials, so entity history survives this update.
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_AUTH_ID: auth_id,
                            CONF_PASSWORD: password,
                        },
                    )
                errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_ID): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(entry: BrouteConfigEntry) -> BrouteOptionsFlow:
        """Return the options flow for the polling interval."""
        return BrouteOptionsFlow()


class BrouteOptionsFlow(OptionsFlow):
    """Let the user change how often the meter is polled."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the polling interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
