"""W2g (user ruling 2026-11-15): a Step0 Save that moves the region rebuilds
Regions' paused whole-slide viewer at once, so entering Step 0.5 does not tear
the old picture down on screen (a visible flash). On screen, or with nothing
moved, nothing extra happens. Stand-ins for the mount: no slide is opened."""
from types import SimpleNamespace

from block01.ui.regions.mount import RegionsViewerHost


class _Mount:
    def __init__(self, paused=True, moved=True, gpu=True):
        self.host = SimpleNamespace(stack=SimpleNamespace(view=None))
        self.paused = paused
        self._moved = moved
        self._pending = None
        self._gpu = gpu
        self.done = []

    @property
    def source_pending_while_paused(self):
        return self.paused and self._pending is not None

    def sync_source(self, reason):
        if not self._moved:
            return False
        self._moved = False
        if self.paused:
            self._pending = reason
            return False
        self.done.append("rebuild on screen")
        return True

    def rebuild_paused(self):
        if not self._gpu:
            return False                     # the CPU picture rebuilds on entry
        self._pending = None
        self.done.append("rebuild paused")
        return True


def _host(mount):
    page = SimpleNamespace(set_view=lambda _v: None)
    host = RegionsViewerHost(SimpleNamespace(), page)
    host.mount = mount
    return host


def test_a_paused_viewer_is_rebuilt_at_the_save():
    m = _Mount()
    assert _host(m).sync("handoff") is True
    assert m.done == ["rebuild paused"] and m.paused and not m.source_pending_while_paused


def test_on_screen_or_unmoved_nothing_more_happens():
    on = _Mount(paused=False)
    _host(on).sync("handoff")
    assert on.done == ["rebuild on screen"]
    still = _Mount(moved=False)
    assert _host(still).sync("handoff") is False and still.done == []


def test_the_cpu_picture_still_waits_for_the_entry():
    m = _Mount(gpu=False)
    _host(m).sync("handoff")
    assert m.done == [] and m.source_pending_while_paused
