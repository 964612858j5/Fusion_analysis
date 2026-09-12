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
  owner, and no QWidget or `ChannelSetModel` is a fallback for it;
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

from block01.core.display_identity import (  # noqa: E402
    ChannelCapabilities, DatasetIdentity, resolve_identity,
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


def test_a_channel_set_model_is_not_a_fallback_for_the_public_answer(app):
    """`ChannelSetModel` is a view's model. If the shared state fell back to
    it, two lists could disagree and the "one answer" would be whichever was
    asked."""
    from block01.ui.widgets.channel_dock import ChannelSetModel, ChannelState

    st = _state(app)
    st.bind(_identity("A"))
    model = ChannelSetModel()
    model.set_channels([ChannelState(channel_id="CD3", visible=True)])
    model.select("CD3")

    assert st.selected_channel() == "", "the model answered for the state"
    assert st.display_visible("CD3") is False


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

def test_an_unresolvable_path_is_marked_rather_than_guessed(tmp_path):
    missing = resolve_identity(str(tmp_path / "not-there.ome.tiff"))
    assert missing is not None
    assert missing.resolved is False
    assert missing.fingerprint == ""

    real = tmp_path / "there.ome.tiff"
    real.write_bytes(b"x" * 10)
    found = resolve_identity(str(real))
    assert found.resolved is True
    assert found != DatasetIdentity(path=str(real), fingerprint="")


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
