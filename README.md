# Sit Solar inverter

Home Assistant custom integration for **Sit Solar / Futura Sun** hybrid
inverters, read over the **Si-Dongle-L** WiFi dongle (TCP port `7078`) using the
[`sitsolar-dongle`](https://github.com/ch3p4ll3/sitsolar-dongle) Python library.

Tested against a **3K-HS** single-phase high-voltage unit. Three-phase and other
models should work — the integration reads the `device_type` register and only
creates the entities that actually exist on your unit — but they are untested
here.

The integration is **read-only**: it polls the dongle and never writes to the
inverter. It is local-only (no cloud) and polls on a configurable interval
(default 30 s, minimum 15 s).

## Installation

### HACS (recommended)

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add this repository URL with category **Integration**.
3. Install **Sit Solar inverter** from the list.
4. Restart Home Assistant.

### Manual

Copy `custom_components/sitsolar/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Configuration

The integration is configured through the UI (**Settings → Devices & Services
→ Add Integration → Sit Solar inverter**):

| Field | Description |
| --- | --- |
| **Host** | IP address or hostname of the Si-Dongle-L dongle. |
| **Dongle serial** | Serial number printed on the dongle. This is the password for the dongle's encrypted protocol (HMAC); it is never logged and is redacted in diagnostics. |
| **Polling interval** | Seconds between polls (default 30, minimum 15). |

On first setup the integration reads the inverter serial and model in a single
frame to validate the connection and name the device. The config entry's unique
ID is the **inverter** serial, so you cannot accidentally add the same inverter
twice.

To change the dongle's IP later, use **Reconfigure** on the config entry. To
change the polling interval, use **Options**. If the dongle rejects the stored
serial (for example after a dongle swap), the integration starts a **reauth**
flow.

## Energy dashboard

To populate the Home Assistant **Energy** dashboard, pick these sensors:

| Energy dashboard slot | Sensor |
| --- | --- |
| **Solar production** | `energy_from_pv_total` (lifetime) or `energy_today_dc` (daily) |
| **Grid import** | `energy_from_grid_total` (lifetime) or `energy_today_buy_from_grid` (daily) |
| **Grid export** | `energy_to_grid_total` (lifetime) or `energy_today_sell_to_grid` (daily) |
| **Battery charge** | `energy_to_battery_total` (lifetime) or `energy_today_battery_charge` (daily) |
| **Battery discharge** | `energy_from_battery_total` (lifetime) or `energy_today_battery_discharge` (daily) |

Use either the lifetime or the daily counter consistently; both are
`total_increasing`, so the recorder handles the daily reset for the `today`
sensors. Lifetime counters that have never left 0 are reported as unavailable
rather than feeding a stuck `0` into your statistics.

## Device overview

In addition to the per-entity sensors, the integration exposes a single device
with:

* **DeviceInfo** — manufacturer, model, inverter serial number, aggregated
  firmware versions (DSP1 / DSP2 / ARM), and a hardware version from
  `device_type`.
* **Diagnostic sensors** (disabled by default) — firmware versions, the dongle
  clock from the handshake, last successful update time, poll latency, and the
  count of unreadable register ranges.

See [`docs/ENTITIES.md`](docs/ENTITIES.md) for the complete entity map,
including the hardware quirks the integration corrects and everything that is
deliberately not exposed.

## Troubleshooting

### Setup fails with "invalid auth" / the dongle rejects the serial

The dongle serial is the HMAC key. Re-check the serial printed on the dongle
(the config flow validates it before creating the entry). If it keeps failing
after a dongle replacement, use the reauth flow to enter the new serial.

### Entities go unavailable every ~75 seconds, or the dongle drops out

The Si-Dongle-L **refuses new TCP connections for about 75 seconds** after a
dropped or closed session (it serves one connection at a time). The integration
accounts for this:

* It keeps the socket **open between polls** (a background reader answers the
  dongle's ~37 s heartbeat), so a healthy connection is never torn down and
  never triggers the lockout.
* A single **request timeout** on an otherwise live connection is retried
  **once** before the update fails, so one WiFi blip does not make the entities
  unavailable for a cycle.
* If the connection is genuinely lost, it backs off for **90 s** — longer than
  the 75 s lockout — before reconnecting, instead of hammering the dongle.

If you still see flapping, make sure nothing else (another poller, a second
Home Assistant instance, the CLI) is connecting to the same dongle at the same
time, and that your polling interval is at least 15 s. Lower the polling
frequency in **Options** if the dongle is slow to answer.

### A whole group of entities is missing

That register block did not answer on your unit (no EPS, no grid meter, or a
single-phase unit), so the integration did not create those entities. Check the
diagnostic sensor **Unreadable register ranges** (enable it under the device's
diagnostic entities) to see which blocks failed.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/ruff check custom_components tests
.venv/bin/mypy custom_components/sitsolar
.venv/bin/python -m pytest tests/ -q
```

Tests use `pytest-homeassistant-custom-component` with a fake dongle client and
a poll builder that encodes values into raw words and runs them through the
library's own `decode()`, so the data reaching the coordinator is shaped exactly
like a real `poll()`.

## License

GPL-3.0-or-later — see [`LICENSE`](LICENSE).

## Disclaimer

This integration is provided as-is, without warranty. It is read-only and never
writes to the inverter, but you use it at your own risk. Verify the readings on
your own unit before relying on them for anything important — see the
"Unverifiable assumptions" section of [`docs/ENTITIES.md`](docs/ENTITIES.md)
for the specific values that could not be confirmed from the protocol
documentation alone.
