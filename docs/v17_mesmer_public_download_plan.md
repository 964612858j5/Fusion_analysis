# Mesmer model: download from DeepCell's public address, no token — plan v1, 2026-11-10

User (TG 2026-11-10): "以后mesmer权重都走这个公开地址" — the Mesmer model is
fetched from DeepCell's public storage from now on, and the docs follow.
The user never managed to get a token from users.deepcell.org (others report
the registration page failing too, deepcell-tf issue #749).

## 1. Facts (checked 2026-11-10)

* SPACEc 0.1.2 (`spacec/tools/_segmentation.py`, `load_mesmer_model`)
  downloads `https://deepcell-data.s3-us-west-1.amazonaws.com/saved-models/MultiplexSegmentation-9.tar.gz`
  with a plain GET, no token. The address answers 200 (Last-Modified
  2022-05-20). This is how the server's
  `/sda1/Fusion/benchmark/spacec/models/Mesmer_model/` copy was made.
* Downloaded on the Mac mini: 96,885,295 bytes, SHA-256
  `25b1babc2ff769a9f9484184073db8bc264d5ab2de8a6f9ce07c289e8b935129`;
  `MultiplexSegmentation/{keras_metadata.pb, saved_model.pb,
  variables/variables.data-00000-of-00001, variables/variables.index}` (and an
  empty `assets/`). All four files equal `envs/fusion_mesmer/models.json`
  (`mesmer_multiplex_segmentation`: size and SHA-256 each). The product's
  `prepare.ensure` converted it; the torch weights' content digest equals the
  manifest's `content_sha256` (`01335d1f…`).
* Today the installer asks `users.deepcell.org/api/getData/` with
  `X-Api-Key: $DEEPCELL_ACCESS_TOKEN` for a signed link
  (`scripts/install_block01.sh:37-38, 513-527`), refuses without a token
  (`:557-560`), and checks that host in the pre-check (`:213`).
* The licence is unchanged by the route: DeepCell ASSET_LICENSE,
  non-commercial academic use only; the installer keeps showing it and asking
  for the answer (`licence_text` / `accept_licence`).

## 2. Changes (closed whitelist)

* `scripts/install_block01.sh`:
  * `DEEPCELL_API` / `DEEPCELL_KEY` → `MESMER_URL` (the address above) and
    `MESMER_SHA256` (the archive's SHA-256 above);
  * `fetch_model mesmer_multiplex_segmentation`: `download "$MESMER_URL"
    "$stage/mesmer.tar.gz" "$MESMER_SHA256"` — the archive is checked before
    it is unpacked; then the same unpack / copy / `verify_into_place` as today
    (every file checked against models.json);
  * the token refusal (`:557-560`) goes; nothing reads
    `DEEPCELL_ACCESS_TOKEN` any more;
  * the pre-check's network list: `https://users.deepcell.org` →
    `https://deepcell-data.s3-us-west-1.amazonaws.com`;
  * `licence_text`: "from DeepCell's public model storage
    (deepcell-data.s3-us-west-1.amazonaws.com)" instead of
    "(users.deepcell.org)"; licence lines unchanged.
* `tests/test_install_scripts.py`: `test_mesmer_without_a_token_stops_before_anything_is_downloaded`
  becomes: with no token set, `ensure_models` shows the licence, asks, and
  calls `download` with `MESMER_URL` and `MESMER_SHA256` (stubbed `download`,
  `tar`, `verify_into_place`; nothing fetched); a new test answers `no` to
  Mesmer's licence in a terminal and nothing is fetched.
* Docs: `docs/user_guide.md:69`, `docs/用户指南.md:70`,
  `docs/install_with_an_ai_agent.md:24-25, 43-45` (no token; the public
  address; `--models FOLDER` stays), `docs/v17_install_plan.md:128, 190-194`
  (a dated note: replaced by this plan), `envs/fusion_mesmer/README.md` if it
  names the token.

Not changed: models.json (same files, same digests), the conversion
(`prepare.ensure`), the engine, Cellpose / StarDist downloads, `--models`.

## 3. Verification

* The new installer test and the existing licence / model-state tests
  (`tests/test_install_scripts.py`, Linux CI; on the Mac with Homebrew bash,
  the known Linux-only cases aside).
* By hand on the Mac: the installer's `fetch_model mesmer_multiplex_segmentation`
  sourced with stubs for everything but `download` / `tar` /
  `verify_into_place`, into a scratch root — it downloads, checks the
  archive's SHA-256 and places four verified files.
* `bash -n`; shellcheck runs in CI.

## 4. Risks and rollback

* The public address could disappear or change; the archive check then
  stops the install with "not the expected one" or "could not download",
  and `--models FOLDER` still works with any verified copy. Rollback:
  revert the commit (back to the token route).
* No change for installs that already have the model (verified files are
  kept).

## 5. Review and execution record (2026-11-10)

Codex plan review (astra low): PASS, no P1/P2. P3 taken: a Mesmer `no`
answer test (the old licence test used StarDist and only "no terminal");
the old install plan's token flow, its acceptance item and its source note
are all marked replaced. Advisory: the archive facts were measured on the
Mac (this plan §1); codex could not reach S3 from its sandbox.

Done: `scripts/install_block01.sh` (MESMER_URL / MESMER_SHA256, the token
refusal and `users.deepcell.org` gone, network check and licence text),
`tests/test_install_scripts.py` (two tests), the user guides, the AI-agent
install doc, the install plan's notes. `bash -n` ok. On the Mac: both new
tests pass; `test_a_licence_needs_an_answer` needs `setsid` (Linux), as on
HEAD.
