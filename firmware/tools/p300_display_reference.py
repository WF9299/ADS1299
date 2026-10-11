"""Copy-only display re-referencing for the P300 monitor."""

from __future__ import annotations

from enum import Enum

import numpy as np

try:
    from .p300_common import CHANNELS
    from .p300_self_test import CONFIG_INTERNAL_SHORT
except ImportError:
    from p300_common import CHANNELS
    from p300_self_test import CONFIG_INTERNAL_SHORT


class ReferenceMode(str, Enum):
    RAW = "raw"
    CAR = "car"
    MEDIAN = "median"


class DisplayReferenceProcessor:
    def __init__(
        self,
        mode: ReferenceMode | str = ReferenceMode.RAW,
        channel_mask: int = 0xFF,
    ) -> None:
        self.mode = ReferenceMode(mode)
        self.channel_mask = 0xFF
        self.set_channel_mask(channel_mask)

    def set_mode(self, mode: ReferenceMode | str) -> bool:
        value = ReferenceMode(mode)
        changed = value != self.mode
        self.mode = value
        return changed

    def set_channel_mask(self, channel_mask: int) -> bool:
        value = int(channel_mask)
        if not 0 <= value <= 0xFF or value.bit_count() < 2:
            raise ValueError("display reference requires at least two selected channels")
        changed = value != self.channel_mask
        self.channel_mask = value
        return changed

    @property
    def selected_channels(self) -> tuple[int, ...]:
        return tuple(index for index in range(CHANNELS) if self.channel_mask & (1 << index))

    def process(self, values: np.ndarray, config_id: int) -> np.ndarray:
        source = np.asarray(values, dtype=np.float64)
        if source.ndim != 2 or source.shape[1] != CHANNELS:
            raise ValueError(f"values must have shape (N, {CHANNELS})")
        output = source.copy()
        if int(config_id) == CONFIG_INTERNAL_SHORT or self.mode == ReferenceMode.RAW:
            return output
        selected = output[:, self.selected_channels]
        if self.mode == ReferenceMode.CAR:
            reference = np.mean(selected, axis=1, keepdims=True)
        else:
            reference = np.median(selected, axis=1, keepdims=True)
        return output - reference
