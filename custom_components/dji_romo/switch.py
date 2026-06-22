"""Switch entities for DJI Romo writable device settings.

These wrap the REST ``PUT .../settings`` endpoint (the ``param`` body schema was
captured from the DJI Home app — see ``client.async_set_settings``). Each switch
mirrors a single on/off key from the ``settings`` GET payload and writes it back
through the coordinator, which patches the cached value optimistically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DjiRomoSwitchDescription(SwitchEntityDescription):
    """Describes a writable settings switch.

    ``param_fn`` builds the ``param`` body for a desired on/off state (it gets the
    coordinator so nested settings can preserve their sibling fields, e.g. keeping
    the Do-Not-Disturb schedule when toggling its ``is_open`` flag); ``value_fn``
    reads the current state from the coordinator (None when unknown).
    """

    value_fn: Callable[[DjiRomoCoordinator], bool | None]
    param_fn: Callable[[DjiRomoCoordinator, bool], dict[str, Any]]


def _setting(coordinator: DjiRomoCoordinator, *path: str) -> Any:
    """Return a value from the REST settings payload by nested key path."""
    current: Any = coordinator.data.cloud_data.get("settings", {})
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _truthy(value: Any) -> bool | None:
    """Coerce a 0/1 setting flag to bool, preserving None when absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _inverted(value: Any) -> bool | None:
    """Coerce an inverted 0/1 flag (1 = feature OFF) to the feature's on/off state."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value == 0
    return None


SWITCHES: tuple[DjiRomoSwitchDescription, ...] = (
    DjiRomoSwitchDescription(
        key="child_lock",
        translation_key="child_lock",
        name="Child Lock",
        icon="mdi:lock",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "is_child_lock_open")
        ),
        param_fn=lambda coordinator, on: {"is_child_lock_open": 1 if on else 0},
    ),
    DjiRomoSwitchDescription(
        key="cliff_detection",
        translation_key="cliff_detection",
        name="Cliff Detection",
        icon="mdi:stairs-down",
        entity_category=EntityCategory.CONFIG,
        # Inverted flag: the app's "void detection" ON maps to is_no_stair_mode = 0
        # ("no-stair mode" off), OFF maps to 1. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _inverted(
            _setting(coordinator, "is_no_stair_mode")
        ),
        param_fn=lambda coordinator, on: {"is_no_stair_mode": 0 if on else 1},
    ),
    DjiRomoSwitchDescription(
        key="battery_care",
        translation_key="battery_care",
        name="Battery Care",
        icon="mdi:battery-heart-variant",
        entity_category=EntityCategory.CONFIG,
        # Caps the resting charge at 80% to slow battery aging. Non-inverted
        # (ON = 1), captured via MITM 2026-06-22. Reads the REST setting (the
        # osd battery_care_active flag is its live mirror).
        value_fn=lambda coordinator: _truthy(_setting(coordinator, "battery_care")),
        param_fn=lambda coordinator, on: {"battery_care": 1 if on else 0},
    ),
    DjiRomoSwitchDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        name="Do Not Disturb",
        icon="mdi:bell-sleep",
        entity_category=EntityCategory.CONFIG,
        # Nested setting: the app sends the whole no_disturb object (the schedule
        # is managed in the app), so we preserve the sibling fields and only flip
        # is_open. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "no_disturb", "is_open")
        ),
        param_fn=lambda coordinator, on: {
            "no_disturb": {
                **(_setting(coordinator, "no_disturb") or {}),
                "is_open": 1 if on else 0,
            }
        },
    ),
    DjiRomoSwitchDescription(
        key="hot_water_mop",
        translation_key="hot_water_mop",
        name="Hot Water Mopping",
        icon="mdi:water-thermometer",
        entity_category=EntityCategory.CONFIG,
        # Washes the mop pads with hot water at the dock. Non-inverted (ON = 1),
        # captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "wash_mop_with_hot_water")
        ),
        param_fn=lambda coordinator, on: {
            "wash_mop_with_hot_water": 1 if on else 0
        },
    ),
    DjiRomoSwitchDescription(
        key="auto_add_cleaner",
        translation_key="auto_add_cleaner",
        name="Auto Add Cleaner",
        icon="mdi:bottle-tonic-plus",
        entity_category=EntityCategory.CONFIG,
        # Nested setting: the app sends the whole add_cleaner_auto object, so we
        # preserve the sibling (sewage_tank_deodorizer) and only flip is_add_in_mop.
        # Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "add_cleaner_auto", "is_add_in_mop")
        ),
        param_fn=lambda coordinator, on: {
            "add_cleaner_auto": {
                **(_setting(coordinator, "add_cleaner_auto") or {}),
                "is_add_in_mop": 1 if on else 0,
            }
        },
    ),
    # --- DISABLED / UNVERIFIED: sewage_tank_deodorizer ------------------------
    # Sibling of is_add_in_mop inside add_cleaner_auto. The key/container are
    # confirmed (seen in the auto-add-cleaner capture), but the ON value (1) is a
    # GUESS, not MITM-confirmed: the app hides this toggle on this device, so it
    # was never observed being set, and a write may be rejected if unsupported.
    # Left commented out (no active entity) until it can be verified.
    # To enable: uncomment this block and re-add a `sewage_tank_deodorizer` name
    # under the switch section of the three translation files (or rely on name=).
    # DjiRomoSwitchDescription(
    #     key="sewage_tank_deodorizer",
    #     translation_key="sewage_tank_deodorizer",
    #     name="Sewage Tank Deodorizer",
    #     icon="mdi:scent",
    #     entity_category=EntityCategory.CONFIG,
    #     value_fn=lambda coordinator: _truthy(
    #         _setting(coordinator, "add_cleaner_auto", "sewage_tank_deodorizer")
    #     ),
    #     param_fn=lambda coordinator, on: {
    #         "add_cleaner_auto": {
    #             **(_setting(coordinator, "add_cleaner_auto") or {}),
    #             "sewage_tank_deodorizer": 1 if on else 0,
    #         }
    #     },
    # ),
    DjiRomoSwitchDescription(
        key="enhanced_particle_cleaning",
        translation_key="enhanced_particle_cleaning",
        name="Enhanced Particle Cleaning",
        icon="mdi:grain",
        entity_category=EntityCategory.CONFIG,
        # Flat key, non-inverted (ON = 1). Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "enhance_particle_clean")
        ),
        param_fn=lambda coordinator, on: {
            "enhance_particle_clean": 1 if on else 0
        },
    ),
    DjiRomoSwitchDescription(
        key="auto_dust_box_drying",
        translation_key="auto_dust_box_drying",
        name="Auto Dust Box Drying",
        icon="mdi:fan",
        entity_category=EntityCategory.CONFIG,
        # Nested setting: the app sends the whole drying object, so we preserve the
        # siblings (mode, auto_enable) and only flip dust_box_drying. This is the
        # on/off setting, not the live drying activity (see the Drying Status
        # sensor). Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "drying", "dust_box_drying")
        ),
        param_fn=lambda coordinator, on: {
            "drying": {
                **(_setting(coordinator, "drying") or {}),
                "dust_box_drying": 1 if on else 0,
            }
        },
    ),
    DjiRomoSwitchDescription(
        key="auto_drying",
        translation_key="auto_drying",
        name="Auto Drying",
        icon="mdi:fan-auto",
        entity_category=EntityCategory.CONFIG,
        # Nested setting in the same drying object as auto_dust_box_drying; the
        # write lock keeps the two from clobbering. Preserves siblings (mode,
        # dust_box_drying), flips auto_enable. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "drying", "auto_enable")
        ),
        param_fn=lambda coordinator, on: {
            "drying": {
                **(_setting(coordinator, "drying") or {}),
                "auto_enable": 1 if on else 0,
            }
        },
    ),
    DjiRomoSwitchDescription(
        key="kitchen_bathroom_wet_wash",
        translation_key="kitchen_bathroom_wet_wash",
        name="Kitchen & Bathroom Wet Wash",
        icon="mdi:water-plus",
        entity_category=EntityCategory.CONFIG,
        # Nested in the wash_back object (shared with the cleaning_frequency select;
        # the write lock keeps them from clobbering). Preserves wash_back_area, flips
        # distinguish_room. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "wash_back", "distinguish_room")
        ),
        param_fn=lambda coordinator, on: {
            "wash_back": {
                **(_setting(coordinator, "wash_back") or {}),
                "distinguish_room": 1 if on else 0,
            }
        },
    ),
    DjiRomoSwitchDescription(
        key="ai_obstacle_recognition",
        translation_key="ai_obstacle_recognition",
        name="AI Obstacle Recognition",
        icon="mdi:eye-check",
        entity_category=EntityCategory.CONFIG,
        # Master toggle of the ai_recognition object (shared with the liquid_response,
        # obstacle_mode and low_clearance selects; the write lock keeps them from
        # clobbering). Preserves siblings, flips is_open. Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "ai_recognition", "is_open")
        ),
        param_fn=lambda coordinator, on: {
            "ai_recognition": {
                **(_setting(coordinator, "ai_recognition") or {}),
                "is_open": 1 if on else 0,
            }
        },
    ),
    DjiRomoSwitchDescription(
        key="obstacle_photo",
        translation_key="obstacle_photo",
        name="Obstacle Photos",
        icon="mdi:camera",
        entity_category=EntityCategory.CONFIG,
        # Flat key, non-inverted (ON = 1). Captured via MITM 2026-06-22.
        value_fn=lambda coordinator: _truthy(
            _setting(coordinator, "obstacle_picture_mode")
        ),
        param_fn=lambda coordinator, on: {"obstacle_picture_mode": 1 if on else 0},
    ),
    # --- DISABLED / UNVERIFIED: auto_wash -------------------------------------
    # "Auto mop wash" is NOT visible in the DJI Home app on this account, so its
    # write body was never captured. The key is known (it still has a read-only
    # "Auto Mop Wash" binary_sensor reading `auto_wash`), and below is a GUESS by
    # analogy with the other wash flags: a flat, non-inverted 0/1 flag (ON = 1).
    # This is NOT MITM-confirmed — it could be inverted or not writable at all,
    # so it is left commented out (no active entity) until it can be verified.
    # To enable: uncomment this block, remove the read-only `auto_mop_wash`
    # binary_sensor, and add an `auto_mop_wash` name under the switch section of
    # strings.json + translations/en.json + translations/fr.json (or rely on the
    # name= below). First confirm the ON value via a MITM capture if possible.
    # DjiRomoSwitchDescription(
    #     key="auto_mop_wash",
    #     translation_key="auto_mop_wash",
    #     name="Auto Mop Wash",
    #     icon="mdi:waves",
    #     entity_category=EntityCategory.CONFIG,
    #     value_fn=lambda coordinator: _truthy(_setting(coordinator, "auto_wash")),
    #     param_fn=lambda coordinator, on: {"auto_wash": 1 if on else 0},
    # ),
    # --- DISABLED / UNVERIFIED: flat flags not visible in the app -------------
    # These settings are not exposed in the DJI Home app on this account, so their
    # write bodies were never captured. Each key is known (each still has a
    # read-only binary_sensor), and below is a GUESS: a flat, non-inverted 0/1 flag
    # (ON = 1). NOT MITM-confirmed — could be inverted or not writable. Left
    # commented out until verified. To enable one: uncomment its block, remove the
    # matching read-only binary_sensor, and add its name under the switch section
    # of the three translation files (or rely on name=).
    # DjiRomoSwitchDescription(
    #     key="enhanced_stain_cleaning",
    #     translation_key="enhanced_stain_cleaning",
    #     name="Enhanced Stain Cleaning",
    #     icon="mdi:liquid-spot",
    #     entity_category=EntityCategory.CONFIG,
    #     value_fn=lambda coordinator: _truthy(_setting(coordinator, "enhance_stain_clean")),
    #     param_fn=lambda coordinator, on: {"enhance_stain_clean": 1 if on else 0},
    # ),
    # DjiRomoSwitchDescription(
    #     key="mop_ozone_deodorizer",
    #     translation_key="mop_ozone_deodorizer",
    #     name="Mop Ozone Deodorizer",
    #     icon="mdi:scent",
    #     entity_category=EntityCategory.CONFIG,
    #     value_fn=lambda coordinator: _truthy(_setting(coordinator, "mop_ozone_deodorizer")),
    #     param_fn=lambda coordinator, on: {"mop_ozone_deodorizer": 1 if on else 0},
    # ),
    # DjiRomoSwitchDescription(
    #     key="status_light",
    #     translation_key="status_light",
    #     name="Status Light",
    #     icon="mdi:led-on",
    #     entity_category=EntityCategory.CONFIG,
    #     # NOTE: instruct_light_status may be an enum (light mode), not a 0/1 flag.
    #     value_fn=lambda coordinator: _truthy(_setting(coordinator, "instruct_light_status")),
    #     param_fn=lambda coordinator, on: {"instruct_light_status": 1 if on else 0},
    # ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo settings switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        DjiRomoSettingSwitch(coordinator, description) for description in SWITCHES
    )


class DjiRomoSettingSwitch(DjiRomoCoordinatorEntity, SwitchEntity):
    """A device setting exposed as a writable switch."""

    entity_description: DjiRomoSwitchDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the current setting state (None when not yet known)."""
        return self.entity_description.value_fn(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            # Pass a builder (not a pre-built dict) so the coordinator evaluates it
            # under its write lock, after any in-flight write's optimistic patch —
            # this keeps switches sharing one nested object from clobbering.
            await self.coordinator.async_set_device_setting(
                lambda: self.entity_description.param_fn(self.coordinator, on)
            )
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to set DJI Romo '{self.name}': {err}"
            ) from err
