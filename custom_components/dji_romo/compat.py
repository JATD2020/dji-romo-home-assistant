"""Compatibility aliases across supported Home Assistant releases."""

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:  # Home Assistant before the callback alias was renamed
    from homeassistant.helpers.entity_platform import (
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,
    )

__all__ = ["AddConfigEntryEntitiesCallback"]
