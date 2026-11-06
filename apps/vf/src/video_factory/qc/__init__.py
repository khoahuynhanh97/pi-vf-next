"""Product Video Grounded Content and Multimodal Quality Control (QC) package."""

from video_factory.qc.evaluator import MultimodalEvaluator, validate_qc_response
from video_factory.qc.image_qc import run_image_qc
from video_factory.qc.models import (
    ImageQCItem,
    NormalizedProduct,
    QCReport,
    QCStage,
    QCVerdict,
)
from video_factory.qc.normalization import normalize_product_identity
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.qc.rubric import (
    build_image_qc_prompt,
    build_video_sampling_qc_prompt,
)
from video_factory.qc.video_qc import run_video_qc

__all__ = [
    "QCStage",
    "QCVerdict",
    "NormalizedProduct",
    "ImageQCItem",
    "QCReport",
    "MultimodalEvaluator",
    "validate_qc_response",
    "QCReportRepository",
    "ProductVideoQCService",
    "run_image_qc",
    "run_video_qc",
    "normalize_product_identity",
    "build_image_qc_prompt",
    "build_video_sampling_qc_prompt",
]
