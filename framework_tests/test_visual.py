from pathlib import Path

from PIL import Image

from calee_regression.visual import compare_screenshot, save_as_baseline


def _make_image(path, color):
    img = Image.new("RGB", (20, 20), color=color)
    img.save(path)


def test_compare_screenshot_identical_images_match(tmp_path):
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    diff_dir = tmp_path / "diffs"

    _make_image(baseline_dir / "shot.png", (100, 150, 200))
    actual_path = actual_dir / "shot.png"
    _make_image(actual_path, (100, 150, 200))

    result = compare_screenshot(actual_path, baseline_dir, "shot", max_diff_ratio=0.01, pixel_threshold=12, diff_dir=diff_dir)

    assert result.match is True
    assert result.diff_ratio == 0.0


def test_compare_screenshot_very_different_images_fail(tmp_path):
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    diff_dir = tmp_path / "diffs"

    _make_image(baseline_dir / "shot.png", (0, 0, 0))
    actual_path = actual_dir / "shot.png"
    _make_image(actual_path, (255, 255, 255))

    result = compare_screenshot(actual_path, baseline_dir, "shot", max_diff_ratio=0.01, pixel_threshold=12, diff_dir=diff_dir)

    assert result.match is False
    assert result.diff_ratio > 0.5
    assert result.diff_path is not None
    assert Path(result.diff_path).exists()


def test_compare_screenshot_no_baseline_is_informational_pass(tmp_path):
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    diff_dir = tmp_path / "diffs"

    actual_path = actual_dir / "shot.png"
    _make_image(actual_path, (10, 20, 30))

    result = compare_screenshot(actual_path, baseline_dir, "shot", max_diff_ratio=0.01, pixel_threshold=12, diff_dir=diff_dir)

    assert result.match is True
    assert result.baseline_path is None
    assert "no baseline" in result.message.lower()


def test_save_as_baseline_copies_file(tmp_path):
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    baseline_dir = tmp_path / "baselines"

    actual_path = actual_dir / "shot.png"
    _make_image(actual_path, (5, 5, 5))

    save_as_baseline(actual_path, baseline_dir, "shot")

    assert (baseline_dir / "shot.png").exists()
