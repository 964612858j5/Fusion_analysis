"""v14.5c.1: PerChannelResolvedSource container (pure structure) tests.

Tests the container/homogeneity logic ONLY. Uses a lightweight namedtuple stub
carrying just kind + source_path + group_name (the fields the container reads for
mixture/identity); constructing a real ResolvedHQSource needs live zarr/loader
handles, which the container logic does not require.
"""

from collections import namedtuple

import pytest

from block01.workers.hq_source_resolver import (
    PerChannelResolvedSource, NotHomogeneousError)
from block01.utils import source_identity as si


# Minimal stub: the container only reads .kind / .source_path / .group_name.
_Src = namedtuple("_Src", ["kind", "source_path", "group_name"])


def _raw(path="/data/raw.ome.tif", group="raw_ome"):
    return _Src("raw_ome", path, group)


def _corr(path="/data/corrected_channels.zarr", group="ROI_1"):
    return _Src("corrected_zarr", path, group)


# 1-3. source_mixture_mode derived from per-channel kinds.
def test_all_raw_homogeneous_raw():
    c = PerChannelResolvedSource({"CD68": _raw(), "CK19": _raw()})
    assert c.source_mixture_mode == "homogeneous_raw"


def test_all_corrected_homogeneous_corrected():
    c = PerChannelResolvedSource({"CD68": _corr(), "CK19": _corr()})
    assert c.source_mixture_mode == "homogeneous_corrected"


def test_mixed_raw_corrected():
    c = PerChannelResolvedSource({"CD68": _corr(), "CK19": _raw()})
    assert c.source_mixture_mode == "mixed_raw_corrected"


# 4. Empty map -> raise.
def test_empty_per_channel_raises():
    with pytest.raises(ValueError):
        PerChannelResolvedSource({})


# 5. homogeneous_single() on identity-homogeneous map returns the single source.
def test_homogeneous_single_returns_single():
    src = _raw()
    c = PerChannelResolvedSource({"CD68": src, "CK19": _raw()})  # same identity
    assert c.is_homogeneous() is True
    got = c.homogeneous_single()
    assert got.kind == "raw_ome"
    assert got.source_path == "/data/raw.ome.tif"


def test_homogeneous_single_corrected_same_group():
    c = PerChannelResolvedSource({"CD68": _corr(group="ROI_1"),
                                  "CK19": _corr(group="ROI_1")})
    assert c.is_homogeneous() is True
    assert c.homogeneous_single().kind == "corrected_zarr"


# 6. Mixed kinds -> raise.
def test_homogeneous_single_mixed_kinds_raises():
    c = PerChannelResolvedSource({"CD68": _corr(), "CK19": _raw()})
    assert c.is_homogeneous() is False
    with pytest.raises(NotHomogeneousError):
        c.homogeneous_single()


# 7. Same kind, different source_path -> raise (identity-based, not mode-based).
def test_homogeneous_single_same_kind_diff_path_raises():
    c = PerChannelResolvedSource({"CD68": _corr(path="/data/a.zarr"),
                                  "CK19": _corr(path="/data/b.zarr")})
    # both corrected -> mixture mode says homogeneous_corrected ...
    assert c.source_mixture_mode == "homogeneous_corrected"
    # ... but identity differs -> homogeneous_single must still raise
    assert c.is_homogeneous() is False
    with pytest.raises(NotHomogeneousError):
        c.homogeneous_single()


# 7b. Same kind + same source_path, different group_name -> raise.
def test_homogeneous_single_same_path_diff_group_raises():
    c = PerChannelResolvedSource({"CD68": _corr(group="ROI_1"),
                                  "CK19": _corr(group="ROI_2")})
    assert c.source_mixture_mode == "homogeneous_corrected"
    assert c.is_homogeneous() is False
    with pytest.raises(NotHomogeneousError):
        c.homogeneous_single()


# 8. Mixture value is one of the registered v14.5a constants.
def test_mixture_mode_uses_registered_constants():
    for entries in (
        {"CD68": _raw()},
        {"CD68": _corr()},
        {"CD68": _corr(), "CK19": _raw()},
    ):
        c = PerChannelResolvedSource(entries)
        assert c.source_mixture_mode in si.SOURCE_MIXTURE_MODES


# 9. No bare `source_shape` field on the container (Q3 naming discipline).
def test_no_bare_source_shape_field():
    c = PerChannelResolvedSource({"CD68": _raw()})
    assert not hasattr(c, "source_shape")
    # the v14.5c.0 ambiguous name must not appear in the class namespace either
    assert "source_shape" not in vars(PerChannelResolvedSource)


# Container is additive: NotHomogeneousError is a ValueError subclass.
def test_not_homogeneous_error_is_value_error():
    assert issubclass(NotHomogeneousError, ValueError)
