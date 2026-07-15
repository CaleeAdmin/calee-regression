from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from .models import VisualDiffResult


def compare_screenshot(actual_path, baseline_dir, name: str, max_diff_ratio: float, pixel_threshold: int, diff_dir) -> VisualDiffResult:
    actual_path = Path(actual_path)
    baseline_path = Path(baseline_dir) / f"{name}.png"

    if not baseline_path.exists():
        return VisualDiffResult(
            match=True,
            diff_ratio=0.0,
            baseline_path=None,
            actual_path=str(actual_path),
            diff_path=None,
            message=(
                f"No baseline found for '{name}' — treated as an informational pass, not a failure. "
                f"A technical owner should review reports/.../screenshots/{name}.png and copy it into "
                f"baselines/ if it should become the new baseline."
            ),
        )

    baseline_img = Image.open(baseline_path).convert("RGB")
    actual_img = Image.open(actual_path).convert("RGB")

    size_note = ""
    if actual_img.size != baseline_img.size:
        size_note = f" (actual size {actual_img.size} resized to baseline size {baseline_img.size} for comparison)"
        actual_img = actual_img.resize(baseline_img.size)

    baseline_arr = np.asarray(baseline_img)
    actual_arr = np.asarray(actual_img)

    diff_array = np.abs(actual_arr.astype(int) - baseline_arr.astype(int)).max(axis=-1)
    differing_mask = diff_array > pixel_threshold
    diff_ratio = float(differing_mask.sum()) / float(differing_mask.size)
    match = diff_ratio <= max_diff_ratio

    diff_dir = Path(diff_dir)
    diff_dir.mkdir(parents=True, exist_ok=True)
    diff_path = diff_dir / f"{name}_diff.png"

    diff_image = baseline_img.convert("L").convert("RGB")
    diff_pixels = np.asarray(diff_image).copy()
    diff_pixels[differing_mask] = [255, 0, 0]
    Image.fromarray(diff_pixels).save(diff_path)

    message = f"diff_ratio={diff_ratio:.4f} (threshold {max_diff_ratio:.4f}), pixel_threshold={pixel_threshold}{size_note}"

    return VisualDiffResult(
        match=match,
        diff_ratio=diff_ratio,
        baseline_path=str(baseline_path),
        actual_path=str(actual_path),
        diff_path=str(diff_path),
        message=message,
    )


def save_as_baseline(actual_path, baseline_dir, name: str) -> None:
    baseline_dir = Path(baseline_dir)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(actual_path, baseline_dir / f"{name}.png")
