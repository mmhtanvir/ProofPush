"""
Visual Regression Comparator.
Performs pixel and structural difference calculations between baseline and proposed screenshots,
handles viewport normalization, dynamic element masking, diff highlighting, and bounding boxes.
"""

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
from PIL import Image, ImageChops, ImageDraw


@dataclass
class VisualDiffBoundingBox:
    x: int
    y: int
    width: int
    height: int


@dataclass
class VisualComparisonResult:
    route: str
    viewport_name: str
    diff_percentage: float  # 0.0 to 1.0 (e.g. 0.05 = 5%)
    passed: bool
    threshold: float
    has_layout_shift: bool = False
    bounding_boxes: List[VisualDiffBoundingBox] = field(default_factory=list)
    diff_image_path: Optional[str] = None
    baseline_image_path: Optional[str] = None
    proposed_image_path: Optional[str] = None
    summary: str = "Visually identical"


class VisualComparator:
    def __init__(self, diff_threshold: float = 0.02, reports_dir: Optional[Path] = None):
        self.diff_threshold = diff_threshold
        self.reports_dir = reports_dir

    def compare_images(
        self,
        baseline_bytes: bytes,
        proposed_bytes: bytes,
        route: str,
        viewport_name: str,
        threshold: Optional[float] = None,
        save_diff: bool = True,
    ) -> VisualComparisonResult:
        """Compare two screenshot byte buffers, calculate difference ratio, bounding boxes, and save diff overlay."""
        eff_threshold = threshold if threshold is not None else self.diff_threshold

        img_b = Image.open(io.BytesIO(baseline_bytes)).convert("RGB")
        img_p = Image.open(io.BytesIO(proposed_bytes)).convert("RGB")

        # Normalize dimensions to maximum width and height
        max_w = max(img_b.width, img_p.width)
        max_h = max(img_b.height, img_p.height)

        canvas_b = Image.new("RGB", (max_w, max_h), (255, 255, 255))
        canvas_b.paste(img_b, (0, 0))

        canvas_p = Image.new("RGB", (max_w, max_h), (255, 255, 255))
        canvas_p.paste(img_p, (0, 0))

        # Pixel difference
        diff = ImageChops.difference(canvas_b, canvas_p)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            diff_data = list(diff.getdata())

        differing_pixels = 0
        total_pixels = max_w * max_h

        # Diff mask to compute bounding box
        diff_mask = Image.new("L", (max_w, max_h), 0)
        mask_pixels = []

        # Color threshold for noticeable visual difference (per RGB channel)
        for r, g, b in diff_data:
            if max(r, g, b) > 25:  # Tolerance threshold to avoid sub-pixel anti-aliasing noise
                differing_pixels += 1
                mask_pixels.append(255)
            else:
                mask_pixels.append(0)

        diff_mask.putdata(mask_pixels)
        diff_ratio = differing_pixels / max(total_pixels, 1)

        # Detect bounding box of changes
        bbox = diff_mask.getbbox()
        bounding_boxes = []
        has_shift = False

        if bbox:
            min_x, min_y, max_x, max_y = bbox
            box_w = max_x - min_x
            box_h = max_y - min_y
            bounding_boxes.append(VisualDiffBoundingBox(x=min_x, y=min_y, width=box_w, height=box_h))
            if box_h > 150 or diff_ratio > 0.10:
                has_shift = True

        passed = (diff_ratio <= eff_threshold)

        diff_path_str = None
        base_path_str = None
        prop_path_str = None

        if save_diff and self.reports_dir:
            clean_route = route.strip("/").replace("/", "_") or "root"
            out_dir = self.reports_dir / "screenshots"
            out_dir.mkdir(parents=True, exist_ok=True)

            base_file = out_dir / f"baseline_{clean_route}_{viewport_name}.png"
            prop_file = out_dir / f"proposed_{clean_route}_{viewport_name}.png"
            diff_file = out_dir / f"diff_{clean_route}_{viewport_name}.png"

            canvas_b.save(base_file, "PNG")
            canvas_p.save(prop_file, "PNG")

            # Create highlighted diff overlay image
            overlay = canvas_p.copy().convert("RGBA")
            draw = ImageDraw.Draw(overlay)
            if bbox:
                draw.rectangle(bbox, outline=(255, 0, 0, 255), width=3)
            # Tint differing pixels bright red/magenta
            tint_data = []
            for m in mask_pixels:
                if m == 255:
                    tint_data.append((255, 0, 100, 160))
                else:
                    tint_data.append((0, 0, 0, 0))
            tint_img = Image.new("RGBA", (max_w, max_h))
            tint_img.putdata(tint_data)
            combined_diff = Image.alpha_composite(overlay, tint_img)
            combined_diff.convert("RGB").save(diff_file, "PNG")

            base_path_str = str(base_file)
            prop_path_str = str(prop_file)
            diff_path_str = str(diff_file)

        summary = (
            f"Visually identical (diff: {diff_ratio:.2%})"
            if passed else
            f"Visual difference detected: {diff_ratio:.2%} altered (threshold: {eff_threshold:.2%})"
        )

        return VisualComparisonResult(
            route=route,
            viewport_name=viewport_name,
            diff_percentage=diff_ratio,
            passed=passed,
            threshold=eff_threshold,
            has_layout_shift=has_shift,
            bounding_boxes=bounding_boxes,
            diff_image_path=diff_path_str,
            baseline_image_path=base_path_str,
            proposed_image_path=prop_path_str,
            summary=summary,
        )
