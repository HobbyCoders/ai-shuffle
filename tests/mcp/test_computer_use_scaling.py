"""Unit tests for computer-use coordinate scaling.

The virtual<->physical mapping is pure math, so we test it by injecting a scale
map directly (no real display needed). Covers the two cases most likely to break
on real hardware: a downscaled high-res primary monitor, and a negative-origin
secondary monitor at a different DPI/scale.
"""

import pytest

from app.mcp.computer_use.scaling import DisplayManager, DisplayInfo


def _mgr_with_map(selected, entry):
    """Build a DisplayManager without touching real displays."""
    mgr = DisplayManager.__new__(DisplayManager)
    mgr.virtual_w, mgr.virtual_h = 1280, 800
    mgr._map = {selected: entry}
    mgr.selected_index = selected
    return mgr


class TestToPhysical:
    def test_primary_downscaled_2x(self):
        # 2560x1600 physical shown as 1280x800 virtual => scale 0.5, origin (0,0).
        mgr = _mgr_with_map(1, {
            "scale": 0.5, "pw": 2560, "ph": 1600,
            "left": 0, "top": 0, "vw": 1280, "vh": 800,
        })
        assert mgr.to_physical(0, 0) == (0, 0)
        assert mgr.to_physical(640, 400) == (1280, 800)
        # Bottom-right virtual pixel maps within physical bounds.
        assert mgr.to_physical(1279, 799) == (2558, 1598)

    def test_negative_origin_secondary(self):
        # Secondary monitor to the LEFT of primary: left=-1920, 100% scale.
        mgr = _mgr_with_map(2, {
            "scale": 1.0, "pw": 1920, "ph": 1080,
            "left": -1920, "top": 0, "vw": 1920, "vh": 1080,
        })
        # Wait: vw/vh capped by capture; here no downscale needed so vw==pw.
        assert mgr.to_physical(0, 0) == (-1920, 0)
        assert mgr.to_physical(960, 540) == (-960, 540)

    def test_mixed_dpi_secondary_downscaled(self):
        # 150%-scaled secondary, physical 3000x2000, top offset, downscaled.
        scale = min(1280 / 3000, 800 / 2000, 1.0)  # = 0.4
        vw, vh = round(3000 * scale), round(2000 * scale)  # 1200, 800
        mgr = _mgr_with_map(2, {
            "scale": scale, "pw": 3000, "ph": 2000,
            "left": 2560, "top": -200, "vw": vw, "vh": vh,
        })
        # Center of virtual maps to center of physical, offset by origin.
        gx, gy = mgr.to_physical(vw / 2, vh / 2)
        assert gx == pytest.approx(2560 + 1500, abs=2)
        assert gy == pytest.approx(-200 + 1000, abs=2)

    def test_requires_screenshot_first(self):
        mgr = _mgr_with_map(1, {
            "scale": 1.0, "pw": 100, "ph": 100,
            "left": 0, "top": 0, "vw": 100, "vh": 100,
        })
        mgr.selected_index = 2  # no map entry for display 2
        with pytest.raises(ValueError, match="screenshot"):
            mgr.to_physical(10, 10)

    def test_out_of_bounds_virtual_is_clamped(self):
        mgr = _mgr_with_map(1, {
            "scale": 0.5, "pw": 2560, "ph": 1600,
            "left": 0, "top": 0, "vw": 1280, "vh": 800,
        })
        # Way past the virtual image — clamps to physical monitor edge.
        gx, gy = mgr.to_physical(99999, 99999)
        assert gx <= 2559 and gy <= 1599
        assert gx >= 0 and gy >= 0


class TestDefaultIndex:
    def test_prefers_primary(self):
        displays = [
            DisplayInfo(index=1, left=-1920, top=0, width=1920, height=1080, primary=False),
            DisplayInfo(index=2, left=0, top=0, width=2560, height=1440, primary=True),
        ]
        assert DisplayManager._default_index(displays) == 2

    def test_falls_back_to_first(self):
        displays = [
            DisplayInfo(index=3, left=0, top=0, width=800, height=600, primary=False),
        ]
        assert DisplayManager._default_index(displays) == 3

    def test_empty_defaults_to_one(self):
        assert DisplayManager._default_index([]) == 1
