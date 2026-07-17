# DJI Romo for Home Assistant

An unofficial Home Assistant integration for DJI Romo robot vacuums.

![DJI Romo](dji.png)

## Features

- Start, pause, stop, locate and return-to-dock controls
- DJI Home cleaning presets and per-room cleaning buttons
- Multi-room cleaning through the `dji_romo.clean_rooms` action
- Cleaning mode, suction, water level, pass count and route controls
- Dust collection, mop washing and mop drying actions
- Robot, dock, tank, consumable and cleaning-history sensors
- Writable robot and dock settings
- Live map and last-cleaning map images
- MQTT push updates with periodic cloud reconciliation and recovery
- Reauthentication flow and a Home Assistant repair notification when credentials fail

## Installation

Home Assistant 2025.1 or newer and HACS are required.

1. Open HACS and select **Custom repositories**.
2. Add `https://github.com/JATD2020/dji-romo-home-assistant` as an **Integration**.
3. Install **DJI Romo**.
4. Restart Home Assistant.
5. Go to **Settings > Devices & services > Add integration** and select **DJI Romo**.

HACS will offer later releases as normal integration updates.

## Credentials

DJI does not currently provide a public Home API for this integration. Use
[dji-home-credential-extractor](https://github.com/xn0tsa/dji-home-credential-extractor)
to retrieve the required user token and robot serial number.

During setup, paste the complete `.env` or `dji_credentials.txt` output into the
credentials field. The integration recognizes these values automatically:

- `DJI_USER_TOKEN`
- `DJI_DEVICE_SN`
- `DJI_API_URL`
- `DJI_LOCALE`

The token and serial number can also be entered separately. Credentials are stored
in the Home Assistant config entry and are only sent to DJI Home endpoints.

If DJI rejects the token later, Home Assistant starts a reauthentication flow. Run
the extractor again and paste the refreshed output into that flow.

## Room cleaning

The integration creates one button per room reported by DJI Home. To clean several
rooms in one job, call `dji_romo.clean_rooms` for the vacuum entity and pass the
room names in the desired order.

Room buttons use the shared room-cleaning controls exposed by the integration:

- Cleaning mode: vacuum and mop, vacuum only, mop only, or vacuum then mop
- Suction power: quiet, standard, or max
- Water level: low, medium, or high
- Cleaning passes
- Route: standard, fast, or fine

## Troubleshooting

- Restart Home Assistant after installing or updating the custom component.
- If setup reports an invalid or expired token, extract fresh credentials and use
  the reauthentication flow.
- Download integration diagnostics from **Settings > Devices & services > DJI Romo**
  when opening an issue. Diagnostics omit credentials, maps, routes and coordinates.
- Include the Home Assistant version, integration version and relevant log entries
  in bug reports.

## Disclaimer

This project is not affiliated with or endorsed by DJI. It uses reverse-engineered,
undocumented cloud endpoints and may stop working when DJI changes its app or
services. Use it at your own risk. Local-only control is not currently supported.
