"""Block01's ONE public channel dock, and the template every row is built from.

`GlobalChannelDock` is the public channel editor for Step0, Step1, Step2 and
Step3: one instance, owned by `Block01DisplayServices`, mounted outside the
stacked pages, projecting `ChannelDisplayState` and `FusionDomainModel`.

WHAT USED TO BE HERE. A reusable `ChannelDock` shell with a per-page row
factory (`Step0ChannelRow`, `WeightChannelRow`, `DisplayChannelRow`) over a
`ChannelSetModel` -- a second, writable copy of colour, visibility and the
display window. Every step built its own dock from those parts, which is how
two lists came to disagree about the same channel. They are gone with B5; the
one dock holds no state of its own and asks the owners instead.
"""

from . import template
from .global_dock import GlobalChannelDock, GlobalChannelRow
from .editors import MinMaxGammaEditor, Step0Inspector, Step3Inspector

__all__ = [
    "template",
    "GlobalChannelDock", "GlobalChannelRow",
    "MinMaxGammaEditor", "Step0Inspector", "Step3Inspector",
]
