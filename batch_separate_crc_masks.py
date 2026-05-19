#!/usr/bin/env python3
"""
Batch preprocess CRC_V2 Cellpose mask files for MacsIQView.

For each first-level result folder under the project root, this script finds:
    */pseudochannel/IO_output_cp_masks.png

It writes the processed mask beside each input file as:
    IO_output_cp_masks_MacsIQView.tif
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import tifffile
from PIL import Image

try:
    from numba import njit, prange
except ImportError:  # pragma: no cover - fallback is for environments without numba
    njit = None
    prange = range


INPUT_MASK_NAME = "IO_output_cp_masks.png"
OUTPUT_MASK_NAME = "IO_output_cp_masks_MacsIQView.tif"
SUMMARY_CSV_NAME = "CRC_mask_preprocessing_summary.csv"
SUMMARY_JSON_NAME = "CRC_mask_preprocessing_summary.json"

SUMMARY_FIELDS = [
    "result_folder",
    "result_folder_path",
    "pseudochannel_path",
    "mask_path",
    "output_path",
    "status",
    "n_masks_found",
    "error_message",
    "start_time",
    "end_time",
    "elapsed_seconds",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


if njit is not None:

    @njit(parallel=True, fastmath=True)
    def separation_border_inplace(image: np.ndarray) -> np.ndarray:
        x, y = image.shape

        for i in prange(1, x - 1):
            for j in range(1, y - 1):
                cur_val = image[i, j]
                if cur_val == 0:
                    continue

                neighbor_top = image[i - 1, j]
                neighbor_bottom = image[i + 1, j]
                neighbor_left = image[i, j - 1]
                neighbor_right = image[i, j + 1]
                neighbor_top_left = image[i - 1, j - 1]
                neighbor_top_right = image[i - 1, j + 1]
                neighbor_bottom_left = image[i + 1, j - 1]
                neighbor_bottom_right = image[i + 1, j + 1]

                if (
                    (neighbor_top != 0 and neighbor_top != cur_val)
                    or (neighbor_bottom != 0 and neighbor_bottom != cur_val)
                    or (neighbor_left != 0 and neighbor_left != cur_val)
                    or (neighbor_right != 0 and neighbor_right != cur_val)
                    or (neighbor_top_left != 0 and neighbor_top_left != cur_val)
                    or (neighbor_top_right != 0 and neighbor_top_right != cur_val)
                    or (neighbor_bottom_left != 0 and neighbor_bottom_left != cur_val)
                    or (neighbor_bottom_right != 0 and neighbor_bottom_right != cur_val)
                ):
                    image[i, j] = 0
        return image

else:

    def separation_border_inplace(image: np.ndarray) -> np.ndarray:
        x, y = image.shape

        for i in range(1, x - 1):
            for j in range(1, y - 1):
                cur_val = image[i, j]
                if cur_val == 0:
                    continue

                neighbors = image[i - 1 : i + 2, j - 1 : j + 2]
                if np.any((neighbors != 0) & (neighbors != cur_val)):
                    image[i, j] = 0
        return image


def load_image(image_path: Path) -> np.ndarray:
    ext = image_path.suffix.lower()

    if ext in {".tif", ".tiff"}:
        image = tifffile.imread(image_path)
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif"}:
        Image.MAX_IMAGE_PIXELS = None
        image = np.array(Image.open(image_path))
    else:
        raise ValueError(f"Unsupported image format: {ext}")

    if image.ndim == 3:
        image = image[..., 0]
    if image.ndim != 2:
        raise ValueError(f"Expected a 2D mask image, got shape {image.shape}")
    return image


def save_image(output_path: Path, image_array: np.ndarray) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(output_path, image_array.astype(np.uint8), compression=True)


def process_mask(mask_path: str | Path, output_path: str | Path) -> None:
    """
    Apply the original Separate_masks.py logic to one mask.

    Processing steps:
    1. Load image.
    2. Remove border pixels where adjacent non-zero labels differ.
    3. Convert all remaining non-zero pixels to 1.
    4. Save as uint8 TIFF.
    """
    mask_path = Path(mask_path)
    output_path = Path(output_path)

    image = load_image(mask_path)
    mask = separation_border_inplace(image.copy())
    mask[mask != 0] = 1
    save_image(output_path, mask)


def base_summary_row(result_folder: Path, n_masks_found: int) -> dict[str, Any]:
    return {
        "result_folder": result_folder.name,
        "result_folder_path": str(result_folder),
        "pseudochannel_path": "",
        "mask_path": "",
        "output_path": "",
        "status": "",
        "n_masks_found": n_masks_found,
        "error_message": "",
        "start_time": "",
        "end_time": "",
        "elapsed_seconds": "",
    }


def finish_row(row: dict[str, Any], start_time: str, start_counter: float) -> dict[str, Any]:
    row["start_time"] = start_time
    row["end_time"] = now_iso()
    row["elapsed_seconds"] = round(perf_counter() - start_counter, 3)
    return row


def worker_process_mask(task: dict[str, str]) -> dict[str, Any]:
    start_time = now_iso()
    start_counter = perf_counter()
    row = dict(task["row"])

    try:
        process_mask(task["mask_path"], task["output_path"])
        row["status"] = "success"
    except Exception as exc:  # noqa: BLE001 - errors must be captured in summary
        row["status"] = "failed"
        row["error_message"] = repr(exc)

    return finish_row(row, start_time, start_counter)


def scan_result_folder(
    result_folder: Path,
    dry_run: bool,
    overwrite: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_time = now_iso()
    start_counter = perf_counter()

    pseudochannels = sorted(
        path for path in result_folder.rglob("pseudochannel") if path.is_dir()
    )
    masks = sorted(
        path
        for path in result_folder.rglob(INPUT_MASK_NAME)
        if path.is_file() and path.parent.name == "pseudochannel"
    )

    logging.info(
        "Scanning result folder: %s | pseudochannels=%d | masks=%d",
        result_folder,
        len(pseudochannels),
        len(masks),
    )

    if not pseudochannels:
        row = base_summary_row(result_folder, 0)
        row["status"] = "missing_pseudochannel"
        return [finish_row(row, start_time, start_counter)], []

    if not masks:
        row = base_summary_row(result_folder, 0)
        row["pseudochannel_path"] = ";".join(str(path) for path in pseudochannels)
        row["status"] = "missing_mask"
        return [finish_row(row, start_time, start_counter)], []

    rows: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    n_masks_found = len(masks)

    for mask_path in masks:
        output_path = mask_path.with_name(OUTPUT_MASK_NAME)
        row = base_summary_row(result_folder, n_masks_found)
        row["pseudochannel_path"] = str(mask_path.parent)
        row["mask_path"] = str(mask_path)
        row["output_path"] = str(output_path)

        if output_path.exists() and not overwrite:
            row["status"] = "already_done"
            rows.append(finish_row(row, start_time, start_counter))
            logging.info("Skipping existing output: %s", output_path)
            continue

        if dry_run:
            row["status"] = "success"
            row["error_message"] = "dry_run: output not written"
            rows.append(finish_row(row, start_time, start_counter))
            logging.info("Dry-run would process: %s", mask_path)
            continue

        tasks.append(
            {
                "mask_path": str(mask_path),
                "output_path": str(output_path),
                "row": row,
            }
        )

    return rows, tasks


def write_csv(rows: list[dict[str, Any]], csv_path: Path) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: list[dict[str, Any]], json_path: Path) -> None:
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2, ensure_ascii=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch preprocess CRC_V2 staged mask files for MacsIQView."
    )
    parser.add_argument(
        "--root",
        default="/mnt/MACSimaDumpling/CRC_V2",
        help="CRC_V2 project root containing first-level result folders.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Parallel workers for mask processing. Recommended: 2-4.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and write summaries only; do not generate TIFF outputs.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Reprocess masks even if IO_output_cp_masks_MacsIQView.tif exists.",
    )
    parser.add_argument(
        "--summary-csv",
        default=None,
        help=f"Summary CSV path. Default: <root>/{SUMMARY_CSV_NAME}",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help=f"Summary JSON path. Default: <root>/{SUMMARY_JSON_NAME}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root).expanduser()
    workers = max(1, min(args.workers, os.cpu_count() or 1))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not root.is_dir():
        raise NotADirectoryError(f"Root directory not found: {root}")

    summary_csv = Path(args.summary_csv) if args.summary_csv else root / SUMMARY_CSV_NAME
    summary_json = (
        Path(args.summary_json) if args.summary_json else root / SUMMARY_JSON_NAME
    )

    result_folders = sorted(path for path in root.iterdir() if path.is_dir())
    logging.info("Root: %s", root)
    logging.info("Found %d first-level result folders", len(result_folders))
    logging.info(
        "Mode: workers=%d | dry_run=%s | overwrite=%s",
        workers,
        args.dry_run,
        args.overwrite,
    )

    summary_rows: list[dict[str, Any]] = []
    process_tasks: list[dict[str, Any]] = []

    for result_folder in result_folders:
        rows, tasks = scan_result_folder(
            result_folder=result_folder,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        summary_rows.extend(rows)
        process_tasks.extend(tasks)

    if process_tasks:
        logging.info("Processing %d mask(s) with %d worker(s)", len(process_tasks), workers)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker_process_mask, task) for task in process_tasks]
            for future in as_completed(futures):
                row = future.result()
                summary_rows.append(row)
                if row["status"] == "success":
                    logging.info("Success: %s", row["mask_path"])
                else:
                    logging.error("Failed: %s | %s", row["mask_path"], row["error_message"])
    else:
        logging.info("No masks need processing")

    summary_rows.sort(
        key=lambda row: (
            row["result_folder_path"],
            row["pseudochannel_path"],
            row["mask_path"],
            row["status"],
        )
    )

    write_csv(summary_rows, summary_csv)
    write_json(summary_rows, summary_json)

    status_counts: dict[str, int] = {}
    for row in summary_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    logging.info("Summary CSV: %s", summary_csv)
    logging.info("Summary JSON: %s", summary_json)
    logging.info("Status counts: %s", status_counts)


if __name__ == "__main__":
    main()
