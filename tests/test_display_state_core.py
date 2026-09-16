"""One owner for Block01's shared display state, and one dataset identity.

Until B2 the shared state keyed everything on `(monotonic generation,
absolute path)`. That single value did two incompatible jobs -- saying which
SLIDE a stored window belongs to, and saying which BINDING a background task
was started under -- so the identity changed every time the user re-opened the
same file. Walking A -> B -> A found an empty namespace and re-seeded every
window from pixels the state already had the answer for, and a late task could
only be judged by a value that had already moved.

What this module pins:

* selection, display visibility, colour and display mapping have ONE writable
  owner, and no QWidget is a fallback for it;
* a whole state installs atomically -- observers see the finished thing, and a
  restore is not a run of user actions;
* A -> B -> A restores A's namespace, while work from A's FIRST binding is
  still refused;
* the namespace store is bounded, and a late callback cannot resurrect an
  evicted one.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.fusion_domain import (  # noqa: E402
    FusionDomainModel,
)

from block01.core.display_identity import (  # noqa: E402
    ChannelCapabilities, DatasetIdentity, ephemeral_identity, resolve_identity,
)
from block01.ui.block01_display import (  # noqa: E402
    Block01DisplayServices, ChannelDisplayState,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _identity(name, fingerprint="1:1"):
    return DatasetIdentity(path=f"/tmp/{name}.ome.tiff", fingerprint=fingerprint)


def _state(app):
    return ChannelDisplayState()


# ── A. one public answer ─────────────────────────────────────────────────

def test_the_shared_state_answers_selection_visibility_colour_and_mapping(app):
    st = _state(app)
    st.bind(_identity("A"))

    st.set_selected_channel("CD3")
    st.set_display_visible("CD3", True)
    st.set_color("CD3", "#123456")
    st.set_mapping("CD3", 5.0, 50.0, 1.2)

    assert st.selected_channel() == "CD3"
    assert st.display_visible("CD3") is True
    assert st.color("CD3") == "#123456"
    assert st.mapping("CD3") == (5.0, 50.0, 1.2)


def test_a_widget_is_not_a_fallback_for_the_public_answer(app):
    """A ROW is a view. If the shared state fell back to one, two views could
    disagree and the "one answer" would be whichever was asked.

    B5 removed the `ChannelSetModel` this used to be written against -- the
    second, writable copy of exactly these fields -- so it is written against
    the public row that replaced it.
    """
    from block01.ui.widgets.channel_dock.global_dock import GlobalChannelRow

    st = _state(app)
    st.bind(_identity("A"))
    row = GlobalChannelRow("CD3")
    row.set_visible_state(True)
    row.set_color("#123456")

    assert st.selected_channel() == "", "a widget answered for the state"
    assert st.display_visible("CD3") is False
    assert st.color("CD3") != "#123456" or not st.has_color("CD3")
    row.deleteLater()


def test_writing_the_same_value_twice_announces_once(app):
    st = _state(app)
    st.bind(_identity("A"))
    seen = []
    st.selection_changed.connect(lambda c: seen.append(("sel", c)))
    st.visibility_changed.connect(lambda c, v: seen.append(("vis", c, v)))
    st.color_changed.connect(lambda c, h: seen.append(("col", c)))
    st.mapping_changed.connect(lambda c: seen.append(("map", c)))

    for _ in range(3):
        st.set_selected_channel("CD3")
        st.set_display_visible("CD3", True)
        st.set_color("CD3", "#123456")
        st.set_mapping("CD3", 1.0, 2.0, 1.0)

    assert seen == [("sel", "CD3"), ("vis", "CD3", True),
                    ("col", "CD3"), ("map", "CD3")]


def test_the_display_model_holds_no_scientific_weight(app):
    """A writable representative weight here would be a second owner of a
    Step1 fusion fact. It belongs to B3's fusion model."""
    st = _state(app)
    assert not hasattr(st, "set_weight")
    assert not hasattr(st, "set_channel_weight")
    assert not hasattr(st, "set_fusion_enabled")
    ns_fields = set(st._ns.__dataclass_fields__)
    assert "weight" not in ns_fields and "weights" not in ns_fields
    assert "fusion_enabled" not in ns_fields
    assert "correction" not in ns_fields and "bg_method" not in ns_fields


def test_capabilities_are_four_separate_facts(app):
    st = _state(app)
    st.bind(_identity("A"), install={
        "order": ("DAPI", "CD3"),
        "capabilities": {
            "DAPI": ChannelCapabilities(is_nucleus=True,
                                        display_toggleable=True,
                                        weight_editable=False,
                                        correction_eligible=False)},
    })
    caps = st.capabilities("DAPI")
    assert caps.is_nucleus is True
    assert caps.display_toggleable is True
    assert caps.weight_editable is False
    assert caps.correction_eligible is False
    assert st.capabilities("CD3").is_nucleus is False


# ── B. atomic install ────────────────────────────────────────────────────

def test_an_install_is_seen_only_when_it_is_whole(app):
    """No observer may catch "the new mapping with the old selection"."""
    st = _state(app)
    st.bind(_identity("A"), install={"selection": "CD3",
                                     "mappings": {"CD3": (1.0, 2.0, 1.0)}})
    seen = []
    st.state_installed.connect(
        lambda b: seen.append((st.selected_channel(),
                               st.mapping("CD8"),
                               st.display_visible("CD8"),
                               st.color("CD8"))))

    st.install({"selection": "CD8",
                "visibility": {"CD8": True},
                "colors": {"CD8": "#abcdef"},
                "mappings": {"CD8": (7.0, 70.0, 1.5)}})

    assert len(seen) == 1, "more than one completion notice"
    assert seen[0] == ("CD8", (7.0, 70.0, 1.5), True, "#abcdef")


def test_an_install_of_what_is_already_there_announces_nothing(app):
    st = _state(app)
    payload = {"selection": "CD3", "visibility": {"CD3": True},
               "mappings": {"CD3": (1.0, 2.0, 1.0)}}
    st.bind(_identity("A"), install=payload)
    seen = []
    st.state_installed.connect(lambda b: seen.append(b))

    assert st.install(dict(payload)) is False
    assert seen == []


def test_an_install_is_not_a_user_action(app):
    """A restore must not tick anything, show anything or start a first
    enable: those are what a CLICK means."""
    st = _state(app)
    st.bind(_identity("A"))
    seen = []
    st.selection_changed.connect(lambda c: seen.append(("sel", c)))
    st.visibility_changed.connect(lambda c, v: seen.append(("vis", c, v)))

    st.install({"selection": "CD3", "visibility": {"CD8": False}})

    assert seen == [], seen
    assert st.selected_channel() == "CD3"
    assert st.display_visible("CD8") is False


def test_a_programmatic_selection_does_not_show_the_channel(app):
    st = _state(app)
    st.bind(_identity("A"))
    st.set_selected_channel("CD3")
    assert st.display_visible("CD3") is False


# ── C. A -> B -> A, and late results ─────────────────────────────────────

def test_going_back_to_a_slide_restores_its_display_state(app):
    st = _state(app)
    a, b = _identity("A"), _identity("B")

    st.bind(a)
    st.set_selected_channel("CD3")
    st.set_display_visible("CD3", True)
    st.set_mapping("CD3", 11.0, 99.0, 1.4)

    st.bind(b)
    assert st.selected_channel() == "", "B inherited A's selection"
    assert st.display_visible("CD3") is False
    assert st.mapping("CD3") is None

    st.bind(a)
    assert st.selected_channel() == "CD3"
    assert st.display_visible("CD3") is True
    assert st.mapping("CD3") == (11.0, 99.0, 1.4)


def test_the_binding_generation_never_repeats(app):
    st = _state(app)
    a, b = _identity("A"), _identity("B")
    gens = [st.bind(a).generation, st.bind(b).generation,
            st.bind(a).generation]
    assert len(set(gens)) == 3
    assert gens == sorted(gens)
    assert st.identity() == a, "the identity is stable across the walk"


def test_a_result_from_the_first_visit_is_refused_after_going_back(app):
    """The identity says A both times. The generation is what tells the two
    visits apart, and it is what refuses the first visit's work."""
    services = Block01DisplayServices()
    st = services.state
    a, b = _identity("A"), _identity("B")
    first = st.bind(a)
    st.bind(b)
    st.bind(a)

    services._on_mapping_seeded({"binding": first, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.mapping("CD3") is None, "A's first binding landed"


def test_a_user_value_survives_a_seed_that_was_already_running(app):
    services = Block01DisplayServices()
    st = services.state
    binding = st.bind(_identity("A"))
    st.set_mapping("CD3", 3.0, 33.0, 1.0, origin="user")

    services._on_mapping_seeded({"binding": binding, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.mapping("CD3") == (3.0, 33.0, 1.0)


def test_a_late_result_cannot_be_re_signed_with_the_current_slide(app):
    """The caller hands back the binding it was given; it has no way to say
    "whatever is current now" -- and a write aimed at another identity lands
    in that identity's namespace, never in this one."""
    st = _state(app)
    a, b = _identity("A"), _identity("B")
    st.bind(a)
    st.bind(b)

    st.set_mapping("CD3", 1.0, 2.0, 1.0, token=a, origin="late")

    assert st.mapping("CD3") is None
    assert st.mapping_in(a, "CD3") == (1.0, 2.0, 1.0)


# ── D. the bounded namespace store ───────────────────────────────────────

def test_the_ninth_dataset_evicts_the_oldest(app):
    st = _state(app)
    first = _identity("D0")
    st.bind(first)
    st.set_mapping("CD3", 1.0, 2.0, 1.0)
    for i in range(1, ChannelDisplayState.NAMESPACE_LIMIT + 1):
        st.bind(_identity(f"D{i}"))

    resident = st.namespace_identities()
    assert len(resident) == ChannelDisplayState.NAMESPACE_LIMIT
    assert first not in resident, "the oldest was not evicted"
    assert st.identity() in resident, "the current slide was evicted"


def test_a_late_callback_cannot_resurrect_an_evicted_namespace(app):
    st = _state(app)
    first = _identity("D0")
    st.bind(first)
    for i in range(1, ChannelDisplayState.NAMESPACE_LIMIT + 1):
        st.bind(_identity(f"D{i}"))
    before = len(st.namespace_identities())

    assert st.set_mapping("CD3", 1.0, 2.0, 1.0, token=first) is False
    assert st.field_revision(("mapping", ("CD3", False)), first) is None

    assert len(st.namespace_identities()) == before
    assert first not in st.namespace_identities()


def test_a_formal_bind_re_admits_an_evicted_slide_as_new(app):
    st = _state(app)
    first = _identity("D0")
    st.bind(first)
    st.set_mapping("CD3", 11.0, 99.0, 1.4)
    st.set_selected_channel("CD3")
    for i in range(1, ChannelDisplayState.NAMESPACE_LIMIT + 1):
        st.bind(_identity(f"D{i}"))

    st.bind(first)

    assert first in st.namespace_identities()
    assert st.mapping("CD3") is None, "an evicted value came back"
    assert st.selected_channel() == ""


def test_eviction_touches_only_display_state(app):
    """The namespace holds display answers. A scientific draft or a committed
    snapshot living beside it must not be evicted with it."""
    services = Block01DisplayServices()
    st = services.state
    scientific = {"groups": {"g": {"CD3": 0.2}}}
    services._scientific_probe = scientific       # a stand-in for B3's model
    st.bind(_identity("D0"))
    for i in range(1, ChannelDisplayState.NAMESPACE_LIMIT + 2):
        st.bind(_identity(f"D{i}"))

    assert services._scientific_probe is scientific
    assert services._scientific_probe["groups"]["g"]["CD3"] == 0.2


# ── identity itself ──────────────────────────────────────────────────────

def test_an_unresolvable_path_has_no_identity(tmp_path):
    """FAIL CLOSED. A path with no readable version is not an identity: two
    sources there cannot be shown to be the same dataset, and a namespace
    keyed on the path alone would restore one's state over the other's."""
    assert resolve_identity(str(tmp_path / "not-there.ome.tiff")) is None

    real = tmp_path / "there.ome.tiff"
    real.write_bytes(b"x" * 10)
    found = resolve_identity(str(real))
    assert found.resolved is True
    assert found.ephemeral is False


def test_an_ephemeral_identity_is_never_reused(tmp_path):
    """Usable now, restorable never: two binds of the same unreadable path
    are two datasets, because nothing can show that they are one."""
    missing = str(tmp_path / "not-there.ome.tiff")
    a = ephemeral_identity(missing)
    b = ephemeral_identity(missing)
    assert a != b
    assert a.ephemeral is True and a.resolved is False
    assert "ephemeral" in str(a)


def test_binding_an_unreadable_path_twice_gives_two_namespaces(app, tmp_path):
    st = _state(app)
    missing = str(tmp_path / "not-there.ome.tiff")

    first = st.bind_dataset(missing)
    st.set_mapping("CD3", 11.0, 99.0, 1.4)
    second = st.bind_dataset(missing)

    assert first.identity != second.identity
    assert st.mapping("CD3") is None, "an unverified source was restored"
    assert len(st.namespace_identities()) == 2


def test_work_from_a_first_unresolved_bind_cannot_land_on_the_second(app, tmp_path):
    services = Block01DisplayServices()
    st = services.state
    missing = str(tmp_path / "not-there.ome.tiff")
    first = st.bind_dataset(missing)
    st.bind_dataset(missing)

    services._on_mapping_seeded({"binding": first, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.mapping("CD3") is None


def test_an_explicit_synthetic_fingerprint_restores_across_binds(app):
    """A synthetic source that WANTS restoration says so, by describing its
    own version. That is a fingerprint, not a guess."""
    st = _state(app)
    synthetic = DatasetIdentity(path="/synthetic/A.ome.tiff",
                                fingerprint="synthetic-v1")
    assert synthetic.resolved is True

    st.bind(synthetic)
    st.set_mapping("CD3", 11.0, 99.0, 1.4)
    st.bind(_identity("B"))
    st.bind(synthetic)

    assert st.mapping("CD3") == (11.0, 99.0, 1.4)


def test_an_unresolved_bind_does_not_touch_a_resolved_dataset(app, tmp_path):
    st = _state(app)
    real = tmp_path / "A.ome.tiff"
    real.write_bytes(b"x" * 10)
    resolved = resolve_identity(str(real))

    st.bind(resolved)
    st.set_mapping("CD3", 5.0, 50.0, 1.0)
    st.bind_dataset(str(tmp_path / "not-there.ome.tiff"))

    assert st.mapping("CD3") is None, "the ephemeral bind saw A's namespace"
    st.bind(resolved)
    assert st.mapping("CD3") == (5.0, 50.0, 1.0), "A was disturbed"


def test_a_path_that_appears_later_gets_a_fresh_resolved_namespace(app, tmp_path):
    st = _state(app)
    path = tmp_path / "late.ome.tiff"

    ephemeral = st.bind_dataset(str(path)).identity
    st.set_mapping("CD3", 1.0, 2.0, 1.0)

    path.write_bytes(b"x" * 10)
    resolved = st.bind_dataset(str(path)).identity

    assert resolved != ephemeral
    assert resolved.resolved is True
    assert st.mapping("CD3") is None


def test_a_rewritten_file_is_a_different_identity(tmp_path):
    path = tmp_path / "slide.ome.tiff"
    path.write_bytes(b"x" * 10)
    before = resolve_identity(str(path))
    path.write_bytes(b"y" * 20)
    after = resolve_identity(str(path))
    assert before != after, "the same name is not the same pixels"


def test_a_binding_needs_a_source_version(app):
    st = _state(app)
    with pytest.raises(ValueError):
        st.bind(None)
    with pytest.raises(ValueError):
        st.bind(DatasetIdentity(path="", fingerprint=""))
    with pytest.raises(ValueError):
        # A path with no version is exactly what must not become a
        # restorable namespace.
        st.bind(DatasetIdentity(path="/tmp/x.ome.tiff", fingerprint=""))


# ── the per-identity latest binding ──────────────────────────────────────

def test_a_result_from_a_still_current_visit_writes_back_silently(app):
    """A1 -> B, with A still resident and not re-bound: A1's answer belongs
    to A and is filed there, announced to nobody."""
    services = Block01DisplayServices()
    st = services.state
    a, b = _identity("A"), _identity("B")
    a1 = st.bind(a)
    st.bind(b)
    seen = []
    st.mapping_changed.connect(lambda c: seen.append(c))

    services._on_mapping_seeded({"binding": a1, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.mapping_in(a, "CD3") == (1.0, 2.0, 1.0)
    assert st.mapping("CD3") is None, "it landed on B"
    assert seen == [], "a silent write announced itself"


def test_a_result_from_a_superseded_visit_is_refused_from_anywhere(app):
    """A1 -> B -> A2 -> C, then A1 returns. Compared only against the CURRENT
    binding, A1 looked like "some other dataset" at that moment and was let
    through -- and A3 would then have restored it."""
    services = Block01DisplayServices()
    st = services.state
    a, b, c = _identity("A"), _identity("B"), _identity("C")
    a1 = st.bind(a)
    st.bind(b)
    st.bind(a)                      # A2 supersedes A1, for ever
    st.bind(c)
    # CD3 is left ABSENT in A on purpose: the only thing that can refuse this
    # result is the binding rule. With a value already there the precondition
    # check would refuse it too, and the test would pass without proving
    # anything about superseded visits.
    assert st.mapping_in(a, "CD3") is None

    services._on_mapping_seeded({"binding": a1, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.mapping_in(a, "CD3") is None, "A1 wrote back into resident A"
    st.bind(a)
    assert st.mapping("CD3") is None, "A3 restored A1's superseded answer"


def test_a_refused_result_does_not_refresh_the_store_ordering(app):
    """BIND-RECENCY: a formal bind is what keeps a namespace alive. Stale
    background activity must not extend an old one's turn."""
    services = Block01DisplayServices()
    st = services.state
    oldest = _identity("D0")
    first = st.bind(oldest)
    for i in range(1, ChannelDisplayState.NAMESPACE_LIMIT):
        st.bind(_identity(f"D{i}"))
    assert oldest == st.namespace_identities()[0]

    services._on_mapping_seeded({"binding": first, "channel": "CD3",
                                 "nucleus": False, "min": 1.0, "max": 2.0,
                                 "gamma": 1.0})

    assert st.namespace_identities()[0] == oldest, "a late result reordered"
    st.bind(_identity("D99"))
    assert oldest not in st.namespace_identities()


def test_a_rewritten_file_is_a_different_identity(tmp_path):
    path = tmp_path / "slide.ome.tiff"
    path.write_bytes(b"x" * 10)
    before = resolve_identity(str(path))
    path.write_bytes(b"y" * 20)
    after = resolve_identity(str(path))
    assert before != after, "the same name is not the same pixels"


def test_a_binding_needs_a_path(app):
    st = _state(app)
    with pytest.raises(ValueError):
        st.bind(None)
    with pytest.raises(ValueError):
        st.bind(DatasetIdentity(path="", fingerprint=""))


# ── the real Step0 rebuild, and the real Step1 restore ───────────────────
#
# Driven through the product entries, not through `install()`: the defects
# these catch were both in the CALLERS -- an adapter reading a list it had
# just emptied, and a restore that put half its answer in QWidgets.

def _step0_page(app):
    """A real Step0 page over a synthetic three-channel loader."""
    import numpy as np
    from block01.ui.step0 import step0_page as sp

    class _Loader:
        filepath = "/tmp/b2-step0.ome.tiff"
        shape = (256, 128)

        @staticmethod
        def channel_names():
            return ["DAPI", "CD3", "CD8"]

        @staticmethod
        def overview_downsample():
            return 8

        def read_region_lowres(self, ch, y0, y1, x0, x1, ds, normalize=False):
            return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1),
                            dtype=np.float32)

    page = sp.Step0Page()
    page.loader = _Loader()
    page.ome_path = _Loader.filepath
    page.nucleus_channel = "DAPI"
    page.patches = []
    return page


def test_the_real_step0_rebuild_installs_the_loaders_order(app):
    """The adapter used to read `page._channel_order`, which it empties at
    the top of the same method -- so the real install was an empty order, no
    capabilities and no visibility at all."""
    page = _step0_page(app)
    try:
        page._rebuild_channel_list()
        st = page.display.state

        assert st.channel_order() == ("DAPI", "CD3", "CD8")

        dapi = st.capabilities("DAPI")
        assert dapi.is_nucleus is True
        assert dapi.display_toggleable is True
        assert dapi.weight_editable is False
        assert dapi.correction_eligible is False

        # ...and the nucleus is not swept by a bulk Show all / Hide all, nor
        # taken out of the fusion: SEPARATE facts, none derived from another.
        assert dapi.bulk_toggleable is False
        assert dapi.fusion_toggleable is False

        for marker in ("CD3", "CD8"):
            caps = st.capabilities(marker)
            assert caps.is_nucleus is False, marker
            # Since B4-A every channel's row checkbox is a DISPLAY toggle;
            # the correction decision is the method combo's.
            assert caps.display_toggleable is True, marker
            assert caps.weight_editable is True, marker
            assert caps.correction_eligible is True, marker
            assert caps.bulk_toggleable is True, marker
            assert caps.fusion_toggleable is True, marker
    finally:
        page.close()


def test_correction_membership_never_becomes_display_visibility(app):
    """CD3 is selected for correction and CD8 is not. That difference is
    about background correction and must not decide either channel's public
    display visibility."""
    page = _step0_page(app)
    try:
        # The CORRECTION method goes to the marker that is NOT the one a
        # fresh slide shows, so "has a method" and "is shown" point at
        # different channels: whichever way the visibility comes out, it
        # cannot have been derived from the correction.
        page._channel_decisions = {"CD8": "tophat"}
        page._channel_methods = {"CD8": "tophat"}
        page._rebuild_channel_list()
        st = page.display.state

        visibility = st.display_visibility()
        # A slide nobody has given display answers for shows DAPI and no
        # marker at all (user ruling, 2026-09-17): in Step0 a click or a tick
        # is what puts a marker on screen. What matters here is unchanged --
        # the channel WITH a correction method is not the one shown.
        assert visibility["CD3"] is False, visibility
        assert visibility["CD8"] is False, visibility
        assert visibility["DAPI"] is True
        # ...and the correction decision is untouched by any of it.
        assert page._channel_decisions["CD8"] == "tophat"
    finally:
        page.close()


def test_a_step0_rebuild_keeps_a_marker_visibility_someone_else_recorded(app):
    page = _step0_page(app)
    try:
        page._rebuild_channel_list()
        st = page.display.state
        st.set_display_visible("CD3", True, origin="step1")
        st.set_display_visible("CD8", False, origin="step1")

        page._channel_methods = {"CD8": "tophat"}
        page._rebuild_channel_list()

        assert st.display_visible("CD3") is True, "a rebuild dropped it"
        assert st.display_visible("CD8") is False, "correction decided it"
    finally:
        page.close()


def test_the_real_step1_restore_registers_display_state_once(app):
    """`ConfigPanel.restore_display_state` used to register only the colours;
    selection and visibility stayed in the QWidgets, where a public getter
    would have had to look them up."""
    from block01.ui.step0.config_panel import ConfigPanel

    services = Block01DisplayServices()
    services.state.bind(_identity("A"))
    panel = ConfigPanel(["DAPI", "CD3", "CD8"], fusion=FusionDomainModel())
    panel.set_display_state(services.state)
    try:
        installs, panel_signals = [], []
        services.state.state_installed.connect(lambda b: installs.append(b))
        # The FORBIDDEN ones: a restore is not a click, so nothing may tick,
        # show, first-enable, edit a weight or dirty the fusion settings.
        # `current_channel_changed` is not in that set -- the current channel
        # really did change, and the host is entitled to hear it once.
        panel.visibility_changed.connect(
            lambda *a: panel_signals.append(("visibility",) + a))
        panel.config_changed.connect(lambda: panel_signals.append(("config",)))
        panel.weight_edited = None      # not a signal on the panel; see rows
        currents = []
        panel.current_channel_changed.connect(currents.append)
        weights_before = {ch: panel.channel_weight(ch)
                          for ch in ("DAPI", "CD3", "CD8")}

        panel.restore_display_state(
            colors={"CD3": "#123456"},
            visibility={"DAPI": True, "CD3": True, "CD8": False},
            current_channel="CD3")

        assert len(installs) == 1, f"{len(installs)} completion notices"
        assert panel_signals == [], panel_signals
        assert currents == ["CD3"], currents
        st = services.state
        assert st.selected_channel() == "CD3"
        assert st.display_visible("CD3") is True
        assert st.display_visible("CD8") is False
        assert st.color("CD3") == "#123456"
        # No scientific side effect: no first-enable, no weight edit.
        assert {ch: panel.channel_weight(ch)
                for ch in ("DAPI", "CD3", "CD8")} == weights_before
        assert all(panel.weight_provenance(ch) != "explicit"
                   for ch in ("DAPI", "CD3", "CD8"))
    finally:
        panel.deleteLater()


def test_a_restore_of_what_is_already_there_announces_nothing(app):
    from block01.ui.step0.config_panel import ConfigPanel

    services = Block01DisplayServices()
    services.state.bind(_identity("A"))
    panel = ConfigPanel(["DAPI", "CD3"], fusion=FusionDomainModel())
    panel.set_display_state(services.state)
    try:
        payload = dict(colors={"CD3": "#123456"},
                       visibility={"CD3": True}, current_channel="CD3")
        panel.restore_display_state(**payload)
        installs = []
        services.state.state_installed.connect(lambda b: installs.append(b))

        panel.restore_display_state(**payload)

        assert installs == []
    finally:
        panel.deleteLater()
