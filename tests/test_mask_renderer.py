import numpy as np

from block01.utils.mask_renderer import (
    extract_mask_boundaries,
    render_mask_overlay,
)


def _sample_inputs():
    fusion = np.zeros((8, 8, 3), dtype=np.uint8)
    fusion[:, :, 0] = 40
    fusion[:, :, 1] = 80
    fusion[:, :, 2] = 120
    masks = np.zeros((8, 8), dtype=np.uint32)
    masks[2:6, 2:6] = 1
    masks[4:7, 5:7] = 2
    return fusion, masks


def test_alpha_zero_without_outline_returns_background():
    fusion, masks = _sample_inputs()
    out = render_mask_overlay(fusion, masks, alpha=0, show_outline=False)
    assert out.dtype == np.uint8
    np.testing.assert_array_equal(out, fusion)


def test_alpha_one_replaces_mask_fill_when_outline_off():
    fusion, masks = _sample_inputs()
    out = render_mask_overlay(fusion, masks, alpha=1, show_outline=False)
    assert out.dtype == np.uint8
    assert np.any(out[masks > 0] != fusion[masks > 0])
    np.testing.assert_array_equal(out[masks == 0], fusion[masks == 0])


def test_outline_on_changes_boundary_pixels():
    fusion, masks = _sample_inputs()
    no_outline = render_mask_overlay(fusion, masks, alpha=0.35, show_outline=False)
    outline = render_mask_overlay(fusion, masks, alpha=0.35, show_outline=True)
    boundaries = extract_mask_boundaries(masks)
    assert boundaries.any()
    assert np.any(outline[boundaries] != no_outline[boundaries])


def test_show_fusion_false_uses_black_background():
    fusion, masks = _sample_inputs()
    out = render_mask_overlay(
        fusion,
        masks,
        alpha=0,
        show_outline=False,
        show_fusion=False,
    )
    assert out.dtype == np.uint8
    np.testing.assert_array_equal(out, np.zeros_like(fusion))
