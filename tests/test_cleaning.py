"""Tests for DJI Romo cleaning option values."""

from custom_components.dji_romo.cleaning import (
    ROOM_CLEAN_MODE_OPTIONS,
    ROOM_ROUTE_OPTIONS,
    migrate_legacy_entry_values,
    migrate_legacy_room_options,
)
from custom_components.dji_romo.const import (
    CONF_ROOM_CLEAN_MODE,
    CONF_ROOM_CLEAN_SPEED,
    CONF_ROOM_FAN_SPEED,
)


def test_confirmed_option_values() -> None:
    """Only confirmed clean modes and route values are exposed."""
    assert set(ROOM_CLEAN_MODE_OPTIONS.values()) == {1, 2, 3, 4}
    assert ROOM_CLEAN_MODE_OPTIONS["Vacuum then Mop"] == 4
    assert ROOM_ROUTE_OPTIONS == {"Standard": 0, "Fast": 1, "Fine": 2}


def test_migrate_legacy_room_options_preserves_labels() -> None:
    """Legacy selections map to the closest confirmed option."""
    cases = (
        ({CONF_ROOM_CLEAN_MODE: 0}, {CONF_ROOM_CLEAN_MODE: 4}),
        ({CONF_ROOM_CLEAN_SPEED: 1}, {CONF_ROOM_CLEAN_SPEED: 2}),
        ({CONF_ROOM_CLEAN_SPEED: 2}, {CONF_ROOM_CLEAN_SPEED: 0}),
        ({CONF_ROOM_CLEAN_SPEED: 3}, {CONF_ROOM_CLEAN_SPEED: 1}),
    )
    for old, expected in cases:
        migrated, changed = migrate_legacy_room_options(old)
        assert migrated == expected
        assert changed


def test_migrate_legacy_room_options_leaves_other_values_untouched() -> None:
    """Migration does not rewrite unrelated config entry data."""
    values = {"user_token": "secret", CONF_ROOM_CLEAN_MODE: 2}
    migrated, changed = migrate_legacy_room_options(values)
    assert migrated == values
    assert not changed


def test_migrate_legacy_room_options_repairs_invalid_values() -> None:
    """Malformed stored values fall back to safe defaults."""
    migrated, changed = migrate_legacy_room_options(
        {CONF_ROOM_CLEAN_MODE: "invalid", CONF_ROOM_CLEAN_SPEED: 99}
    )
    assert migrated[CONF_ROOM_CLEAN_MODE] == 2
    assert migrated[CONF_ROOM_CLEAN_SPEED] == 0
    assert changed


def test_entry_migration_moves_effective_room_values_to_options() -> None:
    """All room values move together and an existing option wins over data."""
    data, options = migrate_legacy_entry_values(
        {
            "user_token": "secret",
            CONF_ROOM_CLEAN_MODE: 0,
            CONF_ROOM_FAN_SPEED: 1,
        },
        {
            "locale": "en_US",
            CONF_ROOM_FAN_SPEED: 3,
            CONF_ROOM_CLEAN_SPEED: 3,
        },
    )

    assert data == {"user_token": "secret"}
    assert options == {
        "locale": "en_US",
        CONF_ROOM_CLEAN_MODE: 4,
        CONF_ROOM_FAN_SPEED: 3,
        CONF_ROOM_CLEAN_SPEED: 1,
    }
