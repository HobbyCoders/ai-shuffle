"""Unit tests for computer-use coordinate scaling.

The virtual<->physical mapping is pure math, so we test it by injecting a scale
map directly (no real display needed). Covers the two cases most likely to break
on real hardware: a downscaled high-res primary monitor, and a negative-origin
secondary monitor at a different DPI/scale.
"""

import pytest

from app.mcp.computer_use.scaling import DisplayInfo, DisplayManager, window_crop


def _mgr_with_map(selected, entry):
    """Build a DisplayManager without touching real displays."""
    mgr = DisplayManager.__new__(DisplayManager)
    mgr.virtual_w, mgr.virtual_h = 1280, 800
    mgr._map = {selected: entry}
    mgr.selected_index = selected
    mgr.list_displays = lambda: [DisplayInfo(selected, entry['left'], entry['top'],
                                            entry['pw'], entry['ph'], selected == 1)]
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

    def test_edge_coordinates_clamp_but_far_ones_are_refused(self):
        mgr = _mgr_with_map(1, {
            "scale": 0.5, "pw": 2560, "ph": 1600,
            "left": 0, "top": 0, "vw": 1280, "vh": 800,
        })
        # On (or a pixel past) the edge clamps onto the monitor.
        assert mgr.to_physical(1280, 800) == (2558, 1598)
        assert mgr.to_physical(-1, -1) == (0, 0)
        # Far outside the image is a wrong coordinate: clamping it would
        # click something else (often the taskbar), so it is refused.
        with pytest.raises(ValueError, match="outside the screenshot") as err:
            mgr.to_physical(99999, 99999)
        assert err.value.code == "INVALID_ARGUMENT"

    def test_layout_change_requires_new_screenshot(self):
        mgr = _mgr_with_map(2, {"scale": 1, "pw": 100, "ph": 100,
                               "left": -100, "top": -100, "vw": 100, "vh": 100})
        mgr.list_displays = lambda: [DisplayInfo(2, 100, 0, 100, 100, False)]
        with pytest.raises(ValueError, match="layout changed"):
            mgr.to_physical(50, 50)
        assert 2 not in mgr._map

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
    def test_nonfinite_coordinates_are_rejected(self, value):
        mgr = _mgr_with_map(1, {"scale": 1, "pw": 100, "ph": 100,
                               "left": 0, "top": 0, "vw": 100, "vh": 100})
        with pytest.raises(ValueError, match="finite"):
            mgr.to_physical(value, 10)


class TestWindowCrop:
    """screenshot(window_id): the image is a window's visible frame, and its
    pixels stay clickable through the crop offset."""

    display = DisplayInfo(2, -1920, 0, 1920, 1080, False)

    def test_inside_is_the_window(self):
        assert window_crop(self.display, (-1500, 100, 800, 600)) == (-1500, 100, 800, 600)

    def test_clipped_to_the_display(self):
        # Hangs off the left and bottom edges.
        assert window_crop(self.display, (-2000, 900, 500, 400)) == (-1920, 900, 420, 180)

    @pytest.mark.parametrize("bounds", [None, (0, 0, 800, 600), (-1500, 100, 0, 600)])
    def test_none_when_minimized_or_elsewhere(self, bounds):
        assert window_crop(self.display, bounds) is None

    def test_crop_offset_maps_image_pixels_to_physical(self):
        # A 800x600 window crop on a negative-origin display, shown at 1:1.
        mgr = _mgr_with_map(2, {"scale": 1.0, "pw": 800, "ph": 600, "left": -1500, "top": 100,
                                "vw": 800, "vh": 600, "display_rect": (-1920, 0, 1920, 1080)})
        mgr.list_displays = lambda: [self.display]
        assert mgr.to_physical(0, 0) == (-1500, 100)
        assert mgr.to_physical(400, 300) == (-1100, 400)
        # Pixels past the crop are not on the image.
        with pytest.raises(ValueError, match="outside the screenshot"):
            mgr.to_physical(1500, 300)

    def test_downscaled_crop(self):
        mgr = _mgr_with_map(1, {"scale": 0.5, "pw": 2000, "ph": 1000, "left": 300, "top": 200,
                                "vw": 1000, "vh": 500, "display_rect": (0, 0, 2560, 1440)})
        mgr.list_displays = lambda: [DisplayInfo(1, 0, 0, 2560, 1440, True)]
        assert mgr.to_physical(500, 250) == (1300, 700)

    def test_layout_check_uses_the_display_not_the_crop(self):
        entry = {"scale": 1.0, "pw": 800, "ph": 600, "left": -1500, "top": 100,
                 "vw": 800, "vh": 600, "display_rect": (-1920, 0, 1920, 1080)}
        mgr = _mgr_with_map(2, dict(entry))
        mgr.list_displays = lambda: [self.display]
        mgr.to_physical(10, 10)  # unchanged layout: no refusal
        mgr.list_displays = lambda: [DisplayInfo(2, -2560, 0, 2560, 1440, False)]
        with pytest.raises(ValueError, match="layout changed"):
            mgr.to_physical(10, 10)

    def test_capture_rejects_a_crop_off_the_selected_display(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        from app.mcp.computer_use import scaling

        pytest.importorskip("PIL.Image")
        monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=None))
        monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)
        mgr = _mgr_with_map(2, {"scale": 1.0, "pw": 1920, "ph": 1080, "left": -1920, "top": 0,
                                "vw": 1920, "vh": 1080})
        mgr.list_displays = lambda: [self.display]
        with pytest.raises(ValueError, match="not inside the selected display"):
            mgr.capture((0, 0, 100, 100))


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


class TestModelImageBudget:
    """Images must reach the model unchanged: fluid shrinks tool images over a
    1568 px long edge and Anthropic's API over ~1.15 MP, which would break the
    pixel mapping recorded at capture time."""

    @pytest.fixture
    def big_monitor(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        from app.mcp.computer_use import scaling

        Image = pytest.importorskip("PIL.Image")
        monitors = [{"left": 0, "top": 0, "width": 3840, "height": 2160},
                    {"left": 0, "top": 0, "width": 3840, "height": 2160}]

        class Capture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            @property
            def monitors(self):
                return monitors

            def grab(self, region):
                img = Image.new("RGB", (region["width"], region["height"]))
                return SimpleNamespace(width=img.width, height=img.height, rgb=img.tobytes())
        monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))
        monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)
        return Image

    def test_screenshot_fits_budget_even_with_a_large_virtual_size(self, big_monitor):
        import base64
        import io

        from app.mcp.computer_use.scaling import MODEL_MAX_EDGE, MODEL_MAX_PIXELS

        mgr = DisplayManager(virtual_size=(4000, 4000))
        png, info = mgr.capture()
        img = big_monitor.open(io.BytesIO(png))
        assert max(img.size) <= MODEL_MAX_EDGE and img.width * img.height <= MODEL_MAX_PIXELS
        assert (info["virtual_width"], info["virtual_height"]) == img.size
        # The recorded mapping matches the image the model sees.
        gx, gy = mgr.to_physical(img.width - 1, img.height - 1)
        assert gx == pytest.approx(3839, abs=3) and gy == pytest.approx(2159, abs=3)
        assert base64.b64encode(png)

    def test_zoom_of_a_large_crop_fits_budget(self, big_monitor):
        import io

        from app.mcp.computer_use.scaling import MODEL_MAX_EDGE, MODEL_MAX_PIXELS

        mgr = DisplayManager()
        mgr.capture()
        png, info = mgr.capture_region([0, 0, 1280, 720])
        img = big_monitor.open(io.BytesIO(png))
        assert max(img.size) <= MODEL_MAX_EDGE and img.width * img.height <= MODEL_MAX_PIXELS
        assert info["zoom_factor"] == pytest.approx(img.width / 1280, abs=0.001)
