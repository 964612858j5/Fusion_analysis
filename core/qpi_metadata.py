"""PhenoCycler Fusion / PerkinElmer QPTIFF metadata (block v16 QP).

A QPTIFF has no OME-XML. Its pixels are read by the existing readers
unchanged (tifffile's ``qpi`` series: channel pages per level, ``CYX``);
only the two facts the OME-XML would carry come from elsewhere:

  * channel names -- each level-0 channel page's ImageDescription is a
    ``<PerkinElmer-QPI-ImageDescription>`` XML; the marker is
    ``<Biomarker>`` (``<Name>`` is the filter, e.g. "Cy5", the fallback),
    else ``ch_NN``;
  * pixel size -- the TIFF ``XResolution``/``YResolution`` tags.

Pure functions of an open ``tifffile.TiffFile``; no Qt, no reader.
"""

import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_UM_PER_RESUNIT = {2: 25400.0, 3: 10000.0}      # INCH, CENTIMETER


class QpiLayoutError(ValueError):
    """A QPTIFF this pipeline cannot read as a multichannel slide."""


def is_qpi(tf) -> bool:
    """A PerkinElmer QPI file (and not an OME-TIFF)."""
    return bool(getattr(tf, "is_qpi", False)) and not tf.ome_metadata


def _page_field(description, tags) -> dict:
    """``{tag: text}`` of the top-level fields of one page's QPI XML."""
    if not description:
        return {}
    try:
        root = ET.fromstring(_XML_DECL.sub("", description, count=1))
    except ET.ParseError:
        return {}
    return {t: (root.findtext(t) or "").strip() for t in tags}


def _check_layout(tf):
    series = tf.series[0]
    if series.axes != "CYX":
        raise QpiLayoutError(
            f"QPTIFF image axes are {series.axes!r}, not 'CYX' "
            "(only multichannel fluorescence QPTIFF is supported)")
    n = int(series.shape[0])
    for k, level in enumerate(series.levels):
        if len(level.pages) != n or int(level.shape[0]) != n:
            raise QpiLayoutError(
                f"QPTIFF pyramid level {k} has {len(level.pages)} pages for {n} channels")
        for page in level.pages:
            page = page.aspage() if hasattr(page, "aspage") else page
            if int(page.samplesperpixel) != 1:
                raise QpiLayoutError("QPTIFF channel pages must be single-sample")
    return series


def qpi_channel_names(tf) -> List[str]:
    """The checked channel names of a QPTIFF, in page order.

    Raises `QpiLayoutError` for a layout other than one single-sample page
    per channel per level, or when two channels resolve to the same name
    (a name is the key of every channel lookup downstream)."""
    series = _check_layout(tf)
    names = []
    for i, page in enumerate(series.levels[0].pages):
        page = page.aspage() if hasattr(page, "aspage") else page
        f = _page_field(page.description, ("Biomarker", "Name"))
        names.append(f.get("Biomarker") or f.get("Name") or f"ch_{i:02d}")
    seen, dup = set(), []
    for name in names:
        if name in seen and name not in dup:
            dup.append(name)
        seen.add(name)
    if dup:
        raise QpiLayoutError(f"QPTIFF has duplicate channel names: {', '.join(dup)}")
    return names


def qpi_physical_size_um(tf) -> Optional[Tuple[float, float]]:
    """``(dy_um, dx_um)`` from the level-0 resolution tags, or None when the
    unit is not a length (no ResolutionUnit) or a value is missing."""
    page = tf.series[0].levels[0].pages[0]
    page = page.aspage() if hasattr(page, "aspage") else page
    tags = page.tags
    unit = tags.get("ResolutionUnit")
    factor = _UM_PER_RESUNIT.get(int(unit.value)) if unit is not None else None
    if factor is None:
        return None
    out = []
    for name in ("YResolution", "XResolution"):
        tag = tags.get(name)
        if tag is None:
            return None
        num, den = tag.value
        if not num or not den:
            return None
        out.append(factor * float(den) / float(num))   # unit/px -> um/px
    return out[0], out[1]
