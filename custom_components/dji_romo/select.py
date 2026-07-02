"""Select entities for DJI Romo room cleaning options."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_CLEAN_SPEED,
    CONF_ROOM_FAN_SPEED,
    CONF_ROOM_WATER_LEVEL,
)
from .coordinator import DjiRomoCoordinator
from .entity import DjiRomoCoordinatorEntity
from .helpers import setting_value as _setting

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DjiRomoSelectDescription(SelectEntityDescription):
    """Entity description for selectable room-cleaning options."""

    option_map: dict[str, int]


SELECTS: tuple[DjiRomoSelectDescription, ...] = (
    DjiRomoSelectDescription(
        key=CONF_ROOM_CLEAN_MODE,
        name="Room Cleaning Mode",
        icon="mdi:robot-vacuum",
        option_map={
            "Vacuum then Mop": 0,
            "Vacuum and Mop": 1,
            "Vacuum Only": 2,
            "Mop Only": 3,
            "Super clean": 4,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_FAN_SPEED,
        name="Room Suction Power",
        icon="mdi:fan",
        option_map={
            "Quiet": 1,
            "Standard": 2,
            "Max": 3,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_WATER_LEVEL,
        name="Room Water Level",
        icon="mdi:water",
        option_map={
            "Low": 1,
            "Medium": 2,
            "High": 3,
        },
    ),
    DjiRomoSelectDescription(
        key=CONF_ROOM_CLEAN_SPEED,
        name="Room Mopping Speed",
        icon="mdi:speedometer",
        option_map={
            "Slow": 1,
            "Standard": 2,
            "Fast": 3,
        },
    ),
)


@dataclass(frozen=True, kw_only=True)
class DjiRomoSettingSelectDescription(SelectEntityDescription):
    """Describes a device-setting select (writes the REST settings endpoint).

    ``param_fn`` builds the ``param`` body for a chosen value (it gets the
    coordinator so nested settings can preserve their sibling fields); ``value_fn``
    reads the current integer value from the coordinator (None when unknown).
    """

    option_map: dict[str, int]
    value_fn: Callable[[DjiRomoCoordinator], int | None]
    param_fn: Callable[[DjiRomoCoordinator, int], dict[str, Any]]


SETTING_SELECTS: tuple[DjiRomoSettingSelectDescription, ...] = (
    DjiRomoSettingSelectDescription(
        key="drying_mode",
        translation_key="drying_mode",
        name="Drying Duration",
        icon="mdi:timer-cog",
        entity_category=EntityCategory.CONFIG,
        # Nested in the drying object (shared with the auto_drying / dust-box-drying
        # switches; the coordinator's write lock keeps them from clobbering).
        # Values captured via MITM 2026-06-22.
        option_map={"Energy Saving": 0, "Standard": 1, "Strong": 2},
        value_fn=lambda coordinator: _setting(coordinator, "drying", "mode"),
        param_fn=lambda coordinator, val: {
            "drying": {**(_setting(coordinator, "drying") or {}), "mode": val}
        },
    ),
    DjiRomoSettingSelectDescription(
        key="cleaning_frequency",
        translation_key="cleaning_frequency",
        name="Cleaning Frequency",
        icon="mdi:water-sync",
        entity_category=EntityCategory.CONFIG,
        # Mop wash-back frequency (app: "Fréquence de nettoyage"), nested in the
        # wash_back object; preserves the sibling distinguish_room. Higher frequency
        # = lower numeric value. Values captured via MITM 2026-06-22.
        option_map={"Water Saving": 3, "Standard": 2, "High": 1},
        value_fn=lambda coordinator: _setting(
            coordinator, "wash_back", "wash_back_area"
        ),
        param_fn=lambda coordinator, val: {
            "wash_back": {
                **(_setting(coordinator, "wash_back") or {}),
                "wash_back_area": val,
            }
        },
    ),
    DjiRomoSettingSelectDescription(
        key="liquid_response",
        translation_key="liquid_response",
        name="Liquid Response",
        icon="mdi:water-alert",
        entity_category=EntityCategory.CONFIG,
        # How the robot reacts to detected liquids, nested in ai_recognition;
        # preserves the siblings (is_open, obstacle_mode, vertical_obstacle_mode).
        # Values captured via MITM 2026-06-22.
        option_map={"Ignore": 0, "Avoid": 1, "Clean": 2},
        value_fn=lambda coordinator: _setting(
            coordinator, "ai_recognition", "liquid_avoid"
        ),
        param_fn=lambda coordinator, val: {
            "ai_recognition": {
                **(_setting(coordinator, "ai_recognition") or {}),
                "liquid_avoid": val,
            }
        },
    ),
    DjiRomoSettingSelectDescription(
        key="obstacle_handling",
        translation_key="obstacle_handling",
        name="Obstacle Handling",
        icon="mdi:traffic-cone",
        entity_category=EntityCategory.CONFIG,
        # ai_recognition.obstacle_mode, nested; preserves the siblings. Values
        # captured via MITM 2026-06-22.
        option_map={"Avoidance Priority": 2, "Standard": 0, "Cleaning Priority": 1},
        value_fn=lambda coordinator: _setting(
            coordinator, "ai_recognition", "obstacle_mode"
        ),
        param_fn=lambda coordinator, val: {
            "ai_recognition": {
                **(_setting(coordinator, "ai_recognition") or {}),
                "obstacle_mode": val,
            }
        },
    ),
    DjiRomoSettingSelectDescription(
        key="low_clearance_mode",
        translation_key="low_clearance_mode",
        name="Low Clearance Cleaning",
        icon="mdi:table-furniture",
        entity_category=EntityCategory.CONFIG,
        # ai_recognition.vertical_obstacle_mode (cleaning under low furniture),
        # nested; preserves the siblings. Values captured via MITM 2026-06-22.
        option_map={"Avoid Low Spaces": 2, "Standard": 0, "Max Coverage": 1},
        value_fn=lambda coordinator: _setting(
            coordinator, "ai_recognition", "vertical_obstacle_mode"
        ),
        param_fn=lambda coordinator, val: {
            "ai_recognition": {
                **(_setting(coordinator, "ai_recognition") or {}),
                "vertical_obstacle_mode": val,
            }
        },
    ),
    DjiRomoSettingSelectDescription(
        key="carpet_behavior",
        translation_key="carpet_behavior",
        name="Carpet Behavior",
        icon="mdi:rug",
        entity_category=EntityCategory.CONFIG,
        # meet_carpet_mode, flat key (what to do on a newly detected carpet).
        # Values captured via MITM 2026-06-22.
        option_map={"Suction Boost": 1, "Cross Carpet": 2, "Avoid Carpet": 3},
        value_fn=lambda coordinator: _setting(coordinator, "meet_carpet_mode"),
        param_fn=lambda coordinator, val: {"meet_carpet_mode": val},
    ),
    DjiRomoSettingSelectDescription(
        key="dust_collect_mode",
        translation_key="dust_collect_mode",
        name="Dust Collection Mode",
        icon="mdi:delete-sweep",
        entity_category=EntityCategory.CONFIG,
        # dust_collect.collect_mode, nested; preserves the sibling schedule
        # (start_hour/start_minute/week_repeat). For Scheduled (3) the app also sets
        # those — exposing the schedule is a later enhancement. Captured 2026-06-22.
        option_map={"Auto": 1, "Manual": 2, "Scheduled": 3},
        value_fn=lambda coordinator: _setting(
            coordinator, "dust_collect", "collect_mode"
        ),
        param_fn=lambda coordinator, val: {
            "dust_collect": {
                **(_setting(coordinator, "dust_collect") or {}),
                "collect_mode": val,
            }
        },
    ),
    DjiRomoSettingSelectDescription(
        key="dust_collect_days",
        translation_key="dust_collect_days",
        name="Dust Collection Days",
        icon="mdi:calendar-week",
        entity_category=EntityCategory.CONFIG,
        # dust_collect.week_repeat is a Mon-first bitmask (Mon=1, Tue=2, Wed=4,
        # Thu=8, Fri=16, Sat=32, Sun=64; captured 2026-06-22). Presets only — an
        # arbitrary mask set in the app (or 0) shows no selection. Nested; preserves
        # the sibling time + collect_mode.
        option_map={
            "Every Day": 127,
            "Weekdays": 31,
            "Weekend": 96,
            "Monday": 1,
            "Tuesday": 2,
            "Wednesday": 4,
            "Thursday": 8,
            "Friday": 16,
            "Saturday": 32,
            "Sunday": 64,
        },
        value_fn=lambda coordinator: _setting(
            coordinator, "dust_collect", "week_repeat"
        ),
        param_fn=lambda coordinator, val: {
            "dust_collect": {
                **(_setting(coordinator, "dust_collect") or {}),
                "week_repeat": val,
            }
        },
    ),
)

# Robot voice language. This is NOT a settings write — it triggers a voicepack
# module upgrade (see coordinator.async_set_voice_language). Only the languages
# whose voicepack module_type was MITM-confirmed (2026-06-22) are offered; the
# robot likely supports more, but their codes are unconfirmed. Label -> code.
VOICE_LANGUAGES: dict[str, str] = {
    "French": "fr",
    "English": "en",
    "Chinese (Simplified)": "zh",
    "Korean": "ko",
    "German": "de",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Romo select entities."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        DjiRomoRoomOptionSelect(coordinator, description) for description in SELECTS
    ]
    entities.extend(
        DjiRomoSettingSelect(coordinator, description)
        for description in SETTING_SELECTS
    )
    entities.append(DjiRomoVoiceLanguageSelect(coordinator))
    async_add_entities(entities)


class DjiRomoRoomOptionSelect(DjiRomoCoordinatorEntity, SelectEntity):
    """Select entity backed by config entry options."""

    entity_description: DjiRomoSelectDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"
        self._attr_options = list(description.option_map)

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        value = self.coordinator.room_cleaning_options[self.entity_description.key]
        for option, option_value in self.entity_description.option_map.items():
            if option_value == value:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        """Persist a selected option."""
        await self.coordinator.async_set_room_cleaning_option(
            self.entity_description.key,
            self.entity_description.option_map[option],
        )


class DjiRomoSettingSelect(DjiRomoCoordinatorEntity, SelectEntity):
    """A device setting exposed as a select (writes the REST settings endpoint)."""

    entity_description: DjiRomoSettingSelectDescription

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        description: DjiRomoSettingSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_sn}_{description.key}"
        self._attr_options = list(description.option_map)

    @property
    def current_option(self) -> str | None:
        """Return the label matching the current setting value (None if unknown)."""
        value = self.entity_description.value_fn(self.coordinator)
        for label, option_value in self.entity_description.option_map.items():
            if option_value == value:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Write the chosen value to the device settings."""
        value = self.entity_description.option_map[option]
        try:
            # Builder evaluated under the coordinator's write lock (see switches).
            await self.coordinator.async_set_device_setting(
                lambda: self.entity_description.param_fn(self.coordinator, value)
            )
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to set DJI Romo '{self.name}': {err}"
            ) from err


class DjiRomoVoiceLanguageSelect(DjiRomoCoordinatorEntity, SelectEntity):
    """Robot voice language — writes a voicepack module upgrade, not a setting.

    Reads the current language from ``settings.device_language`` (the value the
    robot reports once a pack is installed); selecting a new option triggers the
    async voicepack download. The displayed value catches up on later polls.
    """

    _attr_translation_key = "voice_language"
    _attr_icon = "mdi:translate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_voice_language"
        self._attr_options = list(VOICE_LANGUAGES)

    @property
    def current_option(self) -> str | None:
        """Return the label matching the active language code (None if unknown)."""
        code = _setting(self.coordinator, "device_language")
        for label, lang_code in VOICE_LANGUAGES.items():
            if lang_code == code:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Trigger a voicepack upgrade to the chosen language."""
        try:
            await self.coordinator.async_set_voice_language(VOICE_LANGUAGES[option])
        except UpdateFailed as err:
            raise HomeAssistantError(
                f"Failed to set DJI Romo '{self.name}': {err}"
            ) from err
