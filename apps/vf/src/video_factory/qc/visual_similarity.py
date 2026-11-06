"""Visual similarity metrics for keyframe vs reference comparison."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim

logger = logging.getLogger(__name__)


@dataclass
class SimilarityResult:
    scene_id: str
    reference_path: str
    rendered_path: str
    ssim_score: float          # 0.0 to 1.0, higher is better
    mse: float                 # Mean squared error
    psnr: float                # Peak signal-to-noise ratio (dB)
    passes_threshold: bool     # SSIM >= threshold
    notes: list[str] = field(default_factory=list)


class VisualSimilarityChecker:
    """Compare rendered keyframes against master reference using SSIM."""

    def __init__(self, ssim_threshold: float = 0.65) -> None:
        """Initialize checker with minimum SSIM threshold.

        Args:
            ssim_threshold: Minimum SSIM score to pass (0.65 = acceptable, 0.80 = good).
        """
        self.ssim_threshold = ssim_threshold

    def compare(
        self,
        reference_path: Path,
        rendered_path: Path,
        scene_id: str,
    ) -> SimilarityResult:
        """Compare rendered keyframe to reference using SSIM.

        Args:
            reference_path: Master reference image (approved product photo).
            rendered_path: AI-generated keyframe to validate.
            scene_id: Scene identifier for reporting.

        Returns:
            SimilarityResult with SSIM score and pass/fail verdict.
        """
        notes: list[str] = []
        reference_path = Path(reference_path)
        rendered_path = Path(rendered_path)

        if not reference_path.exists():
            logger.warning("Reference image not found: %s", reference_path)
            return SimilarityResult(
                scene_id=scene_id,
                reference_path=str(reference_path),
                rendered_path=str(rendered_path),
                ssim_score=0.0,
                mse=999.0,
                psnr=0.0,
                passes_threshold=False,
                notes=["Reference image not found"],
            )

        if not rendered_path.exists():
            logger.warning("Rendered image not found: %s", rendered_path)
            return SimilarityResult(
                scene_id=scene_id,
                reference_path=str(reference_path),
                rendered_path=str(rendered_path),
                ssim_score=0.0,
                mse=999.0,
                psnr=0.0,
                passes_threshold=False,
                notes=["Rendered image not found"],
            )

        # Load images
        ref_img = cv2.imread(str(reference_path))
        ren_img = cv2.imread(str(rendered_path))

        if ref_img is None:
            logger.error("Failed to decode reference image: %s", reference_path)
            return SimilarityResult(
                scene_id=scene_id,
                reference_path=str(reference_path),
                rendered_path=str(rendered_path),
                ssim_score=0.0,
                mse=999.0,
                psnr=0.0,
                passes_threshold=False,
                notes=["Failed to decode reference image"],
            )

        if ren_img is None:
            logger.error("Failed to decode rendered image: %s", rendered_path)
            return SimilarityResult(
                scene_id=scene_id,
                reference_path=str(reference_path),
                rendered_path=str(rendered_path),
                ssim_score=0.0,
                mse=999.0,
                psnr=0.0,
                passes_threshold=False,
                notes=["Failed to decode rendered image"],
            )

        # Resize to match dimensions if needed
        if ref_img.shape != ren_img.shape:
            notes.append(
                f"Resized rendered from {ren_img.shape[:2]} to match reference {ref_img.shape[:2]}"
            )
            ren_img = cv2.resize(ren_img, (ref_img.shape[1], ref_img.shape[0]))

        # Convert to grayscale for SSIM
        ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
        ren_gray = cv2.cvtColor(ren_img, cv2.COLOR_BGR2GRAY)

        # Calculate SSIM
        ssim_score, _ = ssim(ref_gray, ren_gray, full=True)

        # Calculate MSE and PSNR
        mse = float(np.mean((ref_img.astype(float) - ren_img.astype(float)) ** 2))
        psnr = float(20 * np.log10(255.0 / np.sqrt(mse))) if mse > 0 else 100.0

        passes = float(ssim_score) >= self.ssim_threshold

        if not passes:
            notes.append(f"SSIM {ssim_score:.3f} below threshold {self.ssim_threshold}")

        return SimilarityResult(
            scene_id=scene_id,
            reference_path=str(reference_path),
            rendered_path=str(rendered_path),
            ssim_score=round(float(ssim_score), 3),
            mse=round(mse, 2),
            psnr=round(psnr, 2),
            passes_threshold=passes,
            notes=notes,
        )


def are_images_perceptually_duplicate(
    path1: str | Path,
    path2: str | Path,
    ssim_threshold: float = 0.92,
) -> bool:
    """Check if two image files represent the same visual content (re-encoded, resized).

    Returns True if SSIM of normalized 256x256 grayscale versions >= ssim_threshold.
    Returns False if images represent distinct photos or fail to decode.
    """
    try:
        p1 = Path(path1)
        p2 = Path(path2)
        if not p1.is_file() or not p2.is_file():
            return False
        img1 = cv2.imread(str(p1), cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(str(p2), cv2.IMREAD_GRAYSCALE)
        if img1 is None or img2 is None:
            return False
        img1_norm = cv2.resize(img1, (256, 256))
        img2_norm = cv2.resize(img2, (256, 256))
        score, _ = ssim(img1_norm, img2_norm, full=True)
        return float(score) >= ssim_threshold
    except Exception:
        return False

