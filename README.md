# DJI Romo Home Assistant Integration

Custom Home Assistant integration for the DJI Romo robot vacuum. It is designed as a HACS-compatible custom component and builds on the credential extraction work documented here:

![DJI Romo](dji.png)

- [xn0tsa/dji-home-credential-extractor](https://github.com/xn0tsa/dji-home-credential-extractor)

## What works today

- Config flow in Home Assistant
- Validates a `DJI_USER_TOKEN` against the DJI Home cloud API
- Resolves your Romo serial number from the account if you do not enter it manually
- Fetches temporary MQTT credentials automatically
- Connects to DJI's TLS MQTT broker
- Creates:
  - 1 vacuum entity
  - 3 sensors (`battery`, `status`, `last_update`)
- Supports raw command publishing through `vacuum.send_command`
- Includes configurable topic patterns and command mappings in the options flow

## Current limitation

The authentication and telemetry side is reasonably clear from the reverse-engineered material, but the exact Romo service methods for `start`, `pause`, `dock`, `locate`, and similar actions are still partly inferred.

That means:

- the integration includes sensible default command mappings
- those defaults may need adjustment for your specific Romo firmware/app version
- if a standard button in Home Assistant does not work, `vacuum.send_command` still lets you test raw methods without changing code

## Install through HACS

1. Put this repository on GitHub.
2. In HACS, add it as a custom repository of type `Integration`.
3. Install `DJI Romo`.
4. Restart Home Assistant.
5. Add the integration from `Settings -> Devices & Services`.

## Getting the token

Use the extractor project to retrieve the token and serial number:

1. Follow the instructions in [dji-home-credential-extractor](https://github.com/xn0tsa/dji-home-credential-extractor)
2. Copy the `DJI_USER_TOKEN`
3. Optionally copy `DJI_DEVICE_SN`
4. Add the Home Assistant integration

## Recommended first setup

1. Add the integration with only the token first.
2. Let it auto-discover the Romo serial number.
3. Confirm that battery/status telemetry starts updating.
4. Test a raw command from Developer Tools.

Example raw command:

```yaml
action: vacuum.send_command
target:
  entity_id: vacuum.romo
data:
  command: start_clean
  params: {}
```

If that works, the default mapping is already close enough. If it does not:

1. Open the integration options.
2. Adjust the command mapping JSON.
3. Reload the integration.

## Default MQTT topics

Subscriptions:

- `forward/cr800/thing/product/{device_sn}/#`
- `thing/product/{device_sn}/#`

Commands:

- `forward/cr800/thing/product/{device_sn}/services`

## Example command mapping

```json
{
  "locate": {
    "method": "find_robot"
  },
  "pause": {
    "method": "pause_clean"
  },
  "return_to_base": {
    "method": "back_charge"
  },
  "start": {
    "method": "start_clean"
  },
  "stop": {
    "method": "stop_clean"
  }
}
```

## Reverse-engineering next step

The fastest way to make control fully reliable is to capture the exact MQTT command payloads sent by the DJI Home app while you tap:

- start
- pause
- stop
- return to dock
- locate/find robot

Once those method names and envelope fields are confirmed, the defaults in this integration can be tightened up.
