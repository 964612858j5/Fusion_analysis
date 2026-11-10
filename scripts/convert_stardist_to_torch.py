"""Make the torch weights the StarDist engine loads.

The engine does not convert anything itself (docs/stardist_torch_plan.md §4.5):
`scripts/install_block01.sh` runs this once after the original models are in
place, and a hand run works the same way:

    python scripts/convert_stardist_to_torch.py                  # make if missing
    python scripts/convert_stardist_to_torch.py --force          # remake
    python scripts/convert_stardist_to_torch.py --update-manifest  # new baseline

Source: the manifest entry `stardist_2D_versatile_fluo` (its root, or
BLOCK01_STARDIST_MODEL_PATH). Output: the directory of the entry
`stardist_2D_versatile_fluo_torch` (its root, or BLOCK01_STARDIST_TORCH_PATH).
By default the result must have the manifest's content digest and the manifest
is not touched. `--update-manifest` (on the build machine only, for example
after a weights-format change) accepts any digest and records it.

The conversion reads `weights_best.h5` with h5py: no TensorFlow is needed.
The `.pt` is not part of the repository and is not redistributed with it.
"""
import argparse
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import model_manifest  # noqa: E402
from seg_runner.engines import STARDIST_TORCH_ENTRY, manifest_entry, model_root  # noqa: E402
from seg_runner.stardist_torch import convert, weights  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="remake an existing file")
    ap.add_argument("--update-manifest", action="store_true",
                    help="record the result's content digest as the new baseline")
    args = ap.parse_args(argv)
    entry = manifest_entry(STARDIST_TORCH_ENTRY, model_manifest.MANIFEST)
    src = model_root(manifest_entry(entry["derived_from"], model_manifest.MANIFEST))
    out = pathlib.Path(model_root(entry))
    expected = None if args.update_manifest else entry.get("content_sha256")
    if expected is None and not args.update_manifest:
        print(f"the manifest has no content_sha256 for {STARDIST_TORCH_ENTRY}; "
              "use --update-manifest on the build machine")
        return 1
    try:
        # the expected digest is checked before the one publish point: a source
        # that is not the model the manifest names writes nothing at all
        digest = convert.convert_dir(src, out, force=args.force, expected_digest=expected)
    except (convert.ConvertError, weights.WeightsError) as exc:
        print(exc)
        return 1
    if args.update_manifest:
        model_manifest.set_content_digest(STARDIST_TORCH_ENTRY, digest)
    print(f"{out / weights.WEIGHTS_FILE}: content {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
