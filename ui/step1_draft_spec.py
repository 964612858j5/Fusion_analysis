"""What Step1's whole-slide frame is composed FROM: the draft, not a copy of it.

Block C3 of `docs/step1_rework_plan.md`. C1 settled the arithmetic and C2 the
planning; this says where the numbers come from, and it is the only place
that turns Block01's owners into a composition spec.

TWO OWNERS, NEITHER OF THEM HERE.

* THE SCIENCE is `FusionDomainModel`'s. The viewer composes the DRAFT --
  `effective_config()`, the enabled subset with the draft's own group,
  group-weight and nucleus numbers -- because a user who moves a weight
  expects the picture to move before they save. What a job runs on is the
  COMMITTED snapshot, and nothing here reads it: Search and Generate go on
  asking `committed_snapshot()`, so an unsaved edit changes the screen and
  nothing else.
* THE DISPLAY is `ChannelDisplayState`'s: the colours, the Min/Max/Gamma
  windows, and which channels are ticked -- read in STEP1'S scope, so
  standing in Step0 cannot change what Step1 composes.

THE SAME NUMBERS AS THE PATCH PREVIEW. An overlay channel shows at
`max over groups of gw * w`, the nucleus at its own weight, exactly as
`MainWindow._overlay_weight` computes it for the patch; the fusion takes
`effective_config()` whole, exactly as `_effective_fusion_config` hands it to
the patch. One rule, two viewers.

NO PIXEL WORK. A channel whose window is not in the shared state yet is left
out of `mappings`; the composition names it and the coordinator asks the
shared service for a seed. Nothing here computes a window, and nothing here
writes to either owner.
"""

from ..viewer.step1_compose import MODE_FUSION, MODE_OVERLAY

#: The display scope Step1's own answers live in.
STEP1_SCOPE = "step1"


def _hex_to_rgb01(value):
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return (1.0, 1.0, 1.0)
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 1.0, 1.0)


def overlay_weight(domain, channel):
    """How strongly a ticked channel shows.

    The nucleus carries its own weight rather than a group's, and a channel
    in several groups shows at the strongest of them -- which is what the
    fusion's max-over-groups does with it too. The same function
    `MainWindow._overlay_weight` computes for the patch preview, asked of the
    model rather than of a panel.
    """
    channel = str(channel or "")
    nucleus_channel, nucleus_weight = domain.nucleus()
    if channel and channel == str(nucleus_channel or ""):
        return max(0.0, min(1.0, float(nucleus_weight or 0.0)))
    best = 0.0
    for name, channel_weights in (domain.groups() or {}).items():
        if channel not in (channel_weights or {}):
            continue
        group_weight = float(domain.group_weight(name) or 0.0)
        best = max(best, group_weight * float(channel_weights[channel] or 0.0))
    return max(0.0, min(1.0, best))


def visible_channels(state, scope=STEP1_SCOPE):
    """The channels ticked IN STEP1, in the display state's own order."""
    if state is None:
        return []
    using = getattr(state, "using_scope", None)
    if using is None:
        visibility = dict(state.display_visibility())
    else:
        with using(scope):
            visibility = dict(state.display_visibility())
    order = list(state.channel_order() or visibility)
    return [ch for ch in order if visibility.get(ch)]


def fusion_channels(config):
    """The channels an effective fusion config names, nucleus included."""
    wanted = set()
    for data in (config.get("groups") or {}).values():
        wanted.update(str(ch) for ch in (data.get("channels") or {}))
    nucleus = config.get("nucleus") or {}
    if nucleus.get("channel"):
        wanted.add(str(nucleus["channel"]))
    return sorted(wanted)


def build_spec(domain, state, mode=MODE_OVERLAY, scope=STEP1_SCOPE):
    """The whole composition spec, read from the two owners in one pass.

    Whole, not patched: every caller that wants a new frame builds a new spec
    here, so a window arriving, a colour moving and a weight moving all reach
    the compositor the same way, and no consumer holds a half-updated draft.
    """
    if domain is None:
        return {"mode": mode, "weights": {}, "colors": {}, "mappings": {}}

    if mode == MODE_FUSION:
        config = domain.effective_config()
        groups = {str(name): {str(ch): float(weight or 0.0)
                              for ch, weight in (data.get("channels") or {}).items()}
                  for name, data in (config.get("groups") or {}).items()}
        group_weights = {
            str(name): float(data.get("group_weight", 1.0) or 0.0)
            for name, data in (config.get("groups") or {}).items()}
        nucleus_config = config.get("nucleus") or {}
        nucleus = (str(nucleus_config.get("channel") or ""),
                   float(nucleus_config.get("weight") or 0.0))
        channels = fusion_channels(config)
        spec = {"mode": MODE_FUSION, "groups": groups,
                "group_weights": group_weights, "nucleus": nucleus}
    else:
        # THE TICKED CHANNELS, at the weight the science gives them. A tick
        # is a display answer and the weight is a scientific one; the overlay
        # needs both, and neither is inferred from the other.
        channels = [ch for ch in visible_channels(state, scope)
                    if domain.fusion_enabled(ch) or overlay_weight(domain, ch)]
        spec = {"mode": MODE_OVERLAY,
                "weights": {ch: overlay_weight(domain, ch) for ch in channels}}

    spec["colors"] = {}
    spec["mappings"] = {}
    if state is not None:
        for channel in channels:
            spec["colors"][channel] = _hex_to_rgb01(state.color(channel))
            # A channel with no window yet is LEFT OUT rather than guessed
            # at: `mapping_or_seed` would compute a percentile here, and the
            # frame path does no pixel work (B.3).
            mapping = state.mapping(channel)
            if mapping is not None:
                spec["mappings"][channel] = tuple(float(v) for v in mapping)
    return spec
