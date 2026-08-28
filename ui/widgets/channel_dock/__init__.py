"""Shared channel sidebar (v15 Workstream A).

One reusable dock shell + per-page row/editor variants. The shell owns only
presentation and interaction (search, bulk visibility, selection, scrolling);
scientific semantics stay in each page's adapter.
"""

from .model import ChannelState, ChannelSetModel, SCOPE_PROCESSING, SCOPE_DISPLAY_ONLY
from .dock import ChannelDock
from .rows import ChannelRowBase, Step0ChannelRow, WeightChannelRow, DisplayChannelRow
from .editors import MinMaxGammaEditor, Step0Inspector, Step3Inspector

__all__ = [
    "ChannelState", "ChannelSetModel", "SCOPE_PROCESSING", "SCOPE_DISPLAY_ONLY",
    "ChannelDock",
    "ChannelRowBase", "Step0ChannelRow", "WeightChannelRow", "DisplayChannelRow",
    "MinMaxGammaEditor", "Step0Inspector", "Step3Inspector",
]
