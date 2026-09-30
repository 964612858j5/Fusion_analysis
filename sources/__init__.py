"""Adapters from today's image sources to the `PixelSource` contract
(`core/pixel_source.py`; block A2a).

This package is the composition layer: it may depend on `core/` and on the
Qt-free `viewer/raw_tile_provider.py`; the contract itself depends on
neither. The adapters DELEGATE to the existing optimised readers and change
none of them (plan v2.2 §5.2). A future `NgffSource` lives here too.
"""

from .corrected_zarr import CorrectedZarrSource
from .ome_tiff import OmeTiffSource

__all__ = ["OmeTiffSource", "CorrectedZarrSource"]
