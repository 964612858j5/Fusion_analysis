"""Make the torch weights the Mesmer engine loads, by hand.

Not needed for a deployment: the engine makes them at its first use
(docs/mesmer_torch_plan.md §10). This runs the same `prepare.ensure` --
same lock, CPU-only conversion child, content check and single publish --
from the repository root in the fusion_mesmer environment:

    python scripts/convert_mesmer_to_torch.py                     # make if missing
    python scripts/convert_mesmer_to_torch.py --force             # remake
    python scripts/convert_mesmer_to_torch.py --update-manifest   # new baseline

Source: the manifest entry `mesmer_multiplex_segmentation` (its root, or
DEEPCELL_MESMER_MODEL_PATH). Output: the directory of the entry
`mesmer_multiplex_segmentation_torch` (its root, or BLOCK01_MESMER_TORCH_PATH).
By default the result must have the manifest's content digest and the
manifest is not touched. `--update-manifest` (on the build machine only, for
example after a weights-format change) accepts any digest and records it.

The weights stay under the non-commercial academic licence of the original
(deepcell-auth ASSET_LICENSE): never commit or publish them.
"""
import argparse
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import model_manifest  # noqa: E402
from seg_runner.engines import MESMER_TORCH_ENTRY, manifest_entry, model_root  # noqa: E402
from seg_runner.mesmer_torch import prepare, weights  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="remake an existing file")
    ap.add_argument("--update-manifest", action="store_true",
                    help="record the result's content digest as the new baseline")
    args = ap.parse_args(argv)
    entry = manifest_entry(MESMER_TORCH_ENTRY, model_manifest.MANIFEST)
    src = model_root(manifest_entry(entry["derived_from"], model_manifest.MANIFEST))
    out = pathlib.Path(model_root(entry))
    expected = None if args.update_manifest else entry.get("content_sha256")
    if expected is None and not args.update_manifest:
        print(f"the manifest has no content_sha256 for {MESMER_TORCH_ENTRY}; "
              "use --update-manifest on the build machine")
        return 1
    try:
        prepare.ensure(out, src, expected, print, force=args.force)
        digest = weights.content_digest(weights.read(out / weights.WEIGHTS_FILE))
    except (prepare.PrepareError, weights.WeightsError) as exc:
        print(exc)
        return 1
    if args.update_manifest:
        model_manifest.set_content_digest(MESMER_TORCH_ENTRY, digest)
    elif digest != expected:
        print(f"{out / weights.WEIGHTS_FILE} holds other weights than the manifest names "
              f"(content {digest[:12]}..., want {expected[:12]}...); remake it with --force")
        return 1
    print(f"{out / weights.WEIGHTS_FILE}: content {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
