# EDF Energy (Kraken) for Home Assistant

Brings your EDF Energy (UK) account into Home Assistant using EDF's official API,
which runs on the Kraken platform. Built for EDF Go Electric, it works with any EDF
electricity tariff that has a smart meter.

## What you get

**Account**
- Balance (GBP; positive means in credit)
- API points remaining (diagnostic, off by default)

**Per electricity meter**
- Current rate and next rate (GBP/kWh), next rate change, standing charge (GBP/day)
- Off-peak (binary sensor): on while the cheapest band applies, e.g. 23:00–06:00 on Go Electric
- Tariff (diagnostic, with tariff/product codes and agreement dates)
- Last full day consumption (kWh) and cost (GBP, including standing charge). Smart meter
  data usually arrives a day or two late, so this is the newest complete day, with its date as an attribute
- Last full day peak and off-peak consumption and cost
- Data available to: how far EDF's half-hourly data reaches
- Demand (W) and meter total (kWh), only if your smart meter display sends live data to EDF

**Energy dashboard**

Half-hourly smart meter data arrives late, often the next day, so it is imported into
long-term statistics with the correct timestamps rather than shown as a live sensor. In
**Settings → Dashboards → Energy → Electricity grid → Add consumption**, choose
`EDF electricity consumption <MPAN>`. For cost, choose "Use an entity tracking the total
costs" and pick `EDF electricity consumption cost <MPAN>`.

Peak and off-peak statistics are also written (`EDF electricity consumption peak <MPAN>`,
`... off-peak <MPAN>` and their cost versions). To see the split, add both as consumption
sources instead of the total. Off-peak is the cheapest band of your tariff, e.g. 23:00–06:00
on Go Electric. On a single-rate tariff everything counts as peak.

EDF has its own time-of-use endpoint, but it needs a time-of-use scheme attached to the
tariff, and Go Electric doesn't have one. The integration splits the same half-hourly meter
readings itself, assigning each half hour to the band in force when it starts, as EDF's endpoint does.

On first run the last 30 days are imported (change this under the integration's options).

**Tracked devices**

To see what one appliance costs, such as an EV charger or hot tub, open the integration and
choose **Add tracked device**. Pick its power sensor (W/kW) or energy sensor (Wh/kWh). You
get peak energy, off-peak energy, peak cost, off-peak cost and total cost. Usage is priced at
your EDF rate as it happens and split exactly at rate changes. No API calls are made; it
reuses the cached rates. Totals keep running and survive restarts. History graphs and
statistics give the per-day breakdown, and the energy sensors can be added to the Energy
dashboard's individual devices. Time while Home Assistant is down isn't counted.

## Install

1. In HACS, open the ⋮ menu → **Custom repositories**, add
   `https://github.com/themistoapp/ha-edf-kraken` as an **Integration**.
2. Install **EDF Energy (Kraken)** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → EDF Energy (Kraken)**.

## Signing in

EDF doesn't show API keys in its website or app, so the easiest option is
**Email and password**. The integration uses them once to create an API key, stores only
the key and forgets the password. Logging in with the key also avoids EDF's login
rate limits.

Creating a key replaces any EDF API key you already had, so another tool that uses
the old key will stop working. If you already have a key, choose **I already have an
API key**.

If EDF rejects the key later, Home Assistant asks you to sign in again, which creates a new key.

## API rate limits

EDF's API documentation sets these limits:

| Limit | EDF's figure | What this integration does |
|---|---|---|
| Points per hour (account users) | 50,000 | About 40 an hour (account query measured at 36 points) |
| Complexity per request | 200 | Only small queries, one account at a time |
| Page size | 100 GraphQL / 25,000 REST | Consumption comes from REST in one page |
| Request-specific limits (`KT-CT-1199`) | Get stricter each time they're broken | Stops all requests for 15 minutes; no retries |
| `obtainKrakenToken` (login) | 3 per minute | One token reused until it nearly expires, then refreshed |
| `smartMeterTelemetry` | 125 per hour | Every 2 minutes by default; the minimum setting is 60 seconds |
| `rateLimitInfo` | 10 per minute | Once an hour, inside the account query |

Polling schedule:

| Data | Source | Interval | Points |
|---|---|---|---|
| Account, balance, tariff, points allowance | GraphQL | 1 hour | Yes |
| Unit rates and standing charges | Public REST | 6 hours, and when your tariff changes | No |
| Half-hourly consumption | REST | 30 minutes, newest data only | No |
| Live telemetry (if available) | GraphQL | 2 minutes (configurable, minimum 60s) | Yes, 9 per request |

Requests are spaced at least one second apart. If live telemetry isn't available for your
meter (`KT-GB-4039`), its entities aren't created. If it stops working later, the poll
drops to once every 30 minutes until it recovers.

## Troubleshooting

- **No consumption data**: half-hourly data needs a smart meter that sends readings to
  EDF, and it usually appears the next day. Check the *Data available to* sensor.
- **Diagnostics**: download them from the integration's ⋮ menu. Credentials, MPANs,
  serials and addresses are redacted.

## Development

```sh
python -m venv .venv && .venv/bin/pip install homeassistant pytest-homeassistant-custom-component
.venv/bin/pytest
```

This project is not affiliated with or endorsed by EDF Energy.
