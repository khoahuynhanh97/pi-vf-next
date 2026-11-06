"""Comprehensive Product Image Preprocessing & Multi-Category Artifact Inpainting Engine.

Generically identifies and in-paints all unwanted commercial artifacts from e-commerce media:
- Shop domain names, URLs, and website watermarks (.vn, .com, .net, etc.)
- Third-party seller branding, reseller logos, and watermark stamps
- Promotional stickers, discount tags, price overlays, flash sale ribbons
- Marketplace badges (Shopee Mall, LazMall, TikiNOW, Freeship Xtra, Top 1 Bán Chạy)
- Seller-added post-production spec callout text & marketing bullet overlays
- Contact information (Hotlines, Zalo, phone numbers, email, TikTok/IG handles)
- QR codes, barcodes, guarantee stamps, warranty badges
- Promotional marketing frames and decorative border templates

Protects strictly:
- Authentic manufacturer logos physically embossed/printed on the hardware body
- Functional LCD/screen displays on the product (battery %, clock digits, etc.)
- Product physical geometry, materials, textures, and original color fidelity.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)


class ArtifactCategory(str, enum.Enum):
    SHOP_DOMAIN = "shop_domain"
    SELLER_BRANDING = "seller_branding"
    PROMOTIONAL_TAG = "promotional_tag"
    MARKETPLACE_BADGE = "marketplace_badge"
    SPEC_CALLOUT_OVERLAY = "spec_callout_overlay"
    CONTACT_AND_SOCIAL = "contact_and_social"
    QR_OR_BARCODE = "qr_or_barcode"
    GUARANTEE_STAMP = "guarantee_stamp"
    MARKETING_FRAME_BORDER = "marketing_frame_border"
    GENERIC_WATERMARK = "generic_watermark"
    OTHER_OVERLAY = "other_overlay"


def compute_sha256(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class BoundingBox:
    ymin: int  # 0 - 1000 scale
    xmin: int
    ymax: int
    xmax: int
    label: str = ""
    category: ArtifactCategory = ArtifactCategory.GENERIC_WATERMARK
    confidence: float = 1.0

    def to_pixel_box(self, width: int, height: int, padding_px: int = 6) -> tuple[int, int, int, int]:
        x1 = max(0, int(self.xmin * width / 1000) - padding_px)
        y1 = max(0, int(self.ymin * height / 1000) - padding_px)
        x2 = min(width, int(self.xmax * width / 1000) + padding_px)
        y2 = min(height, int(self.ymax * height / 1000) + padding_px)
        return x1, y1, x2, y2


@dataclass(frozen=True)
class CleanedImageResult:
    asset_id: str
    original_path: str
    cleaned_path: str
    detected_watermarks: tuple[str, ...] = ()
    detected_categories: tuple[str, ...] = ()
    has_cleaned_text: bool = False
    clean_sha256: str = ""
    status: str = "clean"  # "cleaned", "clean", "unknown", "failed"
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


GENERIC_DETECTION_SYSTEM_PROMPT = """You are an expert Computer Vision OCR and Commercial Media Quality Inspector specialized in e-commerce product asset cleaning.

Your task is to detect ALL unwanted non-product artifacts, seller overlays, promotional elements, and graphic distractions in the product image so they can be seamlessly removed prior to high-fidelity AI video generation.

DETECTION SCOPE (FLAG ALL OF THESE):
1. 'shop_domain': Web domains, URLs (e.g. 'mivietnam.vn', 'shopee.vn', 'lazada.vn', '.com', '.vn', '.net', '.store').
2. 'seller_branding': Third-party seller/shop names, dealer logos, store watermarks, seller avatars.
3. 'promotional_tag': Discount stickers ('-50%', 'Giảm sốc', 'Flash Sale', 'Voucher 50k', 'Mua 1 tặng 1', 'Giá chỉ từ...'), price tags, star ratings, sold count ('Đã bán 10k+').
4. 'marketplace_badge': Platform stamps ('Shopee Mall', 'LazMall', 'TikiNOW', 'Freeship Xtra', 'Chính Hãng 100%', 'Top 1 Bán Chạy').
5. 'spec_callout_overlay': Post-production text callouts and spec bubbles added by graphic designers (e.g. 'Công suất 50W', 'Pin trâu 10000mAh', 'LED siêu sáng', 'Gập gọn 180 độ').
6. 'contact_and_social': Phone numbers, hotlines, Zalo, WhatsApp, Facebook, TikTok, Instagram handles.
7. 'qr_or_barcode': QR codes, barcodes, authenticity verification codes.
8. 'guarantee_stamp': Warranty shields, guarantee medals ('Bảo hành 12 tháng', '1 đổi 1 trong 30 ngày', 'Cam kết hoàn tiền').
9. 'marketing_frame_border': Decorative promotional frame borders or template ribbons surrounding the image perimeter.
10. 'generic_watermark': Semi-transparent watermark patterns or copyright text/symbols (©, ®).

STRICT EXCLUSIONS (DO NOT FLAG OR REMOVE):
- Authentic manufacturer brand logos physically printed, embossed, laser-etched, or molded on the physical hardware body of the product (e.g. 'Xiaomi', 'MI', 'Tribit', 'Apple', 'Sony', 'Baseus', 'Jisulife').
- Functional device screens/displays (e.g. digital clock displays, battery percentage LCD numbers on powerbanks).
- The actual physical product, its accessories, buttons, cords, and charging base.

SCHEMA FORMAT:
Return strictly valid JSON in this exact structure:
{
  "has_unwanted_text": <true | false>,
  "detected_texts": ["<string of text found>"],
  "bounding_boxes": [
    {
      "box_2d": [<ymin 0-1000>, <xmin 0-1000>, <ymax 0-1000>, <xmax 0-1000>],
      "label": "<description or text content>",
      "category": "<shop_domain | seller_branding | promotional_tag | marketplace_badge | spec_callout_overlay | contact_and_social | qr_or_barcode | guarantee_stamp | marketing_frame_border | generic_watermark | other_overlay>",
      "confidence": <0.0 to 1.0>
    }
  ],
  "reason": "<short summary of all artifacts detected>"
}"""


def _inpaint_box_pil(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Inpaint a bounding box region using multi-scale adaptive background synthesis and feathered blending."""
    x1, y1, x2, y2 = box
    w, h = img.size
    if x2 <= x1 or y2 <= y1:
        return

    box_w = x2 - x1
    box_h = y2 - y1

    # Dynamic border thickness based on box dimensions
    border_thickness = max(8, min(40, int(max(box_w, box_h) * 0.25)))
    bx1 = max(0, x1 - border_thickness)
    by1 = max(0, y1 - border_thickness)
    bx2 = min(w, x2 + border_thickness)
    by2 = min(h, y2 + border_thickness)

    # Crop neighborhood region
    outer_region = img.crop((bx1, by1, bx2, by2))
    outer_w, outer_h = outer_region.size

    inner_x1 = x1 - bx1
    inner_y1 = y1 - by1
    inner_x2 = x2 - bx1
    inner_y2 = y2 - by1

    # Synthesize background fill from surrounding border
    # Pass 1: Multi-scale blur on the outer ring to estimate ambient background texture
    blur_radius = max(4.0, border_thickness * 0.8)
    blurred_fill = outer_region.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Pass 2: Secondary fine-detail smoothing
    blurred_fill = blurred_fill.filter(ImageFilter.MedianFilter(size=5))

    # Create feathered alpha mask for smooth seamless boundary blending
    mask = Image.new("L", (outer_w, outer_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([inner_x1, inner_y1, inner_x2, inner_y2], fill=255)
    
    # Smooth feathering radius
    feather_radius = max(2, min(8, int(border_thickness * 0.3)))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=feather_radius))

    # Composite inpainting patch
    outer_region.paste(blurred_fill, (0, 0), mask)

    # Paste back onto source image
    img.paste(outer_region, (bx1, by1))


class ProductImageCleaner:
    """Comprehensive intelligent product image preprocessor that cleans watermarks and seller text."""

    def __init__(
        self,
        *,
        evaluator_fn: Callable[[str, str, str], dict[str, Any]] | None = None,
        output_dir: Path | str | None = None,
        fake_mode: bool = False,
        categories_to_clean: Sequence[str | ArtifactCategory] | None = None,
        max_box_area_ratio: float = 0.25,
        min_confidence: float = 0.75,
    ) -> None:
        self.evaluator_fn = evaluator_fn
        self.output_dir = Path(output_dir) if output_dir else None
        self.fake_mode = fake_mode
        self.categories_to_clean = (
            {
                (c.value if isinstance(c, ArtifactCategory) else str(c)).lower()
                for c in categories_to_clean
            }
            if categories_to_clean
            else None
        )
        self.max_box_area_ratio = max_box_area_ratio
        self.min_confidence = min_confidence

    def _default_vision_detector(self, image_path: str, prompt: str) -> dict[str, Any]:
        """Call Vertex AI / Gemini Vision to detect all artifact bounding boxes."""
        if self.fake_mode:
            img_name = Path(image_path).name.lower()
            if any(k in img_name for k in ("watermark", "shop", "mivietnam", "badge", "promo", "banner", "overlay")):
                return {
                    "has_unwanted_text": True,
                    "detected_texts": ["mivietnam.vn", "Flash Sale 50%"],
                    "bounding_boxes": [
                        {
                            "box_2d": [850, 700, 980, 980],
                            "label": "mivietnam.vn",
                            "category": "shop_domain",
                            "confidence": 0.98,
                        },
                        {
                            "box_2d": [20, 20, 120, 300],
                            "label": "Flash Sale 50%",
                            "category": "promotional_tag",
                            "confidence": 0.95,
                        },
                    ],
                    "reason": "Found shop domain watermark and promotional discount badge",
                }
            return {
                "has_unwanted_text": False,
                "detected_texts": [],
                "bounding_boxes": [],
                "reason": "Clean product image",
            }

        try:
            try:
                from agent.vertex_adapter import get_vertex_config
            except ImportError:
                import sys
                for extra in (Path(r"D:\work\hermes-agent"), Path(r"D:\work\hermes-upstream-rearchitecture")):
                    if extra.is_dir() and str(extra) not in sys.path:
                        sys.path.insert(0, str(extra))
                from agent.vertex_adapter import get_vertex_config
            import httpx

            token, base_url = get_vertex_config()
            if not token or not base_url:
                logger.warning("Vertex credentials unavailable for artifact detection; skipping AI detection")
                return {
                    "detector_status": "unavailable",
                    "has_unwanted_text": False,
                    "detected_texts": [],
                    "bounding_boxes": [],
                    "error": "Vertex credentials unavailable",
                }

            p = Path(image_path)
            img_bytes = p.read_bytes()
            mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            b64_data = base64.b64encode(img_bytes).decode("ascii")

            url = f"{base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            payload = {
                "model": "google/gemini-2.5-flash",
                "messages": [
                    {"role": "system", "content": GENERIC_DETECTION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64_data}"},
                            },
                        ],
                    },
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            resp = httpx.post(url, headers=headers, json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            cleaned_text = content.strip()
            if cleaned_text.startswith("```"):
                lines = cleaned_text.splitlines()
                if len(lines) >= 3 and lines[-1].strip() == "```":
                    cleaned_text = "\n".join(lines[1:-1]).strip()
            result = json.loads(cleaned_text)
            result["detector_status"] = "completed"
            return result
        except Exception as exc:
            logger.warning("Generic artifact AI detection failed for %s: %s", image_path, exc)
            return {
                "detector_status": "failed",
                "has_unwanted_text": False,
                "detected_texts": [],
                "bounding_boxes": [],
                "error": str(exc),
            }

    def clean_image(
        self,
        image_path: str | Path,
        asset_id: str = "",
        output_dir: Path | None = None,
        brand: str = "",
        model: str = "",
    ) -> CleanedImageResult:
        """Inspect and clean a single product image of all commercial overlays and watermarks."""
        p = Path(image_path).resolve()
        if not p.is_file():
            return CleanedImageResult(
                asset_id=asset_id or p.stem,
                original_path=str(p),
                cleaned_path="",
                status="failed",
                error_message=f"File not found: {p}",
            )

        asset_id = asset_id or p.stem
        out_dir = output_dir or self.output_dir or p.parent / "cleaned"
        out_dir.mkdir(parents=True, exist_ok=True)
        cleaned_path = out_dir / f"{asset_id}_clean.png"

        # 1. Detect all categories of unwanted overlays
        prompt = (
            f"Product Brand: {brand or 'Unknown'}, Model: {model or 'Unknown'}. "
            f"Identify all third-party shop domains (.vn, .com), seller logos, promo tags, "
            f"marketplace badges (Shopee/Lazada/Tiki), spec callouts, hotlines, warranty medals, "
            f"or decorative borders. Do NOT flag authentic '{brand}' physical hardware logos."
        )

        try:
            if self.evaluator_fn is not None:
                detection = self.evaluator_fn(str(p), prompt, GENERIC_DETECTION_SYSTEM_PROMPT)
            else:
                detection = self._default_vision_detector(str(p), prompt)
        except Exception as exc:
            logger.warning("Artifact detector failed for %s: %s", p, exc)
            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(p),
                status="unknown",
                error_message=f"Artifact detector failed: {exc}",
                clean_sha256=compute_sha256(p),
                metadata={"detector_status": "failed", "error": str(exc)},
            )

        if not isinstance(detection, Mapping):
            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(p),
                status="unknown",
                error_message="Artifact detector returned an invalid response",
                clean_sha256=compute_sha256(p),
            )
        detector_status = str(detection.get("detector_status", "completed")).lower()
        if detector_status != "completed":
            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(p),
                status="unknown",
                error_message=str(detection.get("error") or f"Artifact detector status: {detector_status}"),
                clean_sha256=compute_sha256(p),
                metadata=dict(detection),
            )

        has_unwanted = bool(detection.get("has_unwanted_text", False))
        detected_texts = tuple(str(t) for t in detection.get("detected_texts", []))
        raw_boxes = detection.get("bounding_boxes", [])

        # Filter boxes by requested categories if specified
        boxes: list[BoundingBox] = []
        found_categories: set[str] = set()

        for b in raw_boxes:
            box_arr = b.get("box_2d") or b.get("box") or []
            if len(box_arr) == 4:
                cat_str = str(b.get("category", "generic_watermark")).lower()
                found_categories.add(cat_str)
                if self.categories_to_clean is not None and cat_str not in self.categories_to_clean:
                    continue
                confidence = float(b.get("confidence", 1.0))
                ymin, xmin, ymax, xmax = (int(value) for value in box_arr)
                if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
                    continue
                area_ratio = ((ymax - ymin) * (xmax - xmin)) / 1_000_000
                if confidence < self.min_confidence:
                    continue
                if area_ratio > self.max_box_area_ratio:
                    return CleanedImageResult(
                        asset_id=asset_id,
                        original_path=str(p),
                        cleaned_path=str(p),
                        detected_watermarks=detected_texts,
                        detected_categories=tuple(sorted(found_categories)),
                        status="unknown",
                        error_message=(
                            f"Unsafe inpaint region for {b.get('label', cat_str)!r}: "
                            f"area ratio {area_ratio:.3f} exceeds {self.max_box_area_ratio:.3f}"
                        ),
                        clean_sha256=compute_sha256(p),
                        metadata=dict(detection),
                    )
                try:
                    category = ArtifactCategory(cat_str)
                except ValueError:
                    category = ArtifactCategory.GENERIC_WATERMARK

                boxes.append(
                    BoundingBox(
                        ymin=ymin,
                        xmin=xmin,
                        ymax=ymax,
                        xmax=xmax,
                        label=str(b.get("label", "")),
                        category=category,
                        confidence=confidence,
                    )
                )

        # 2. If clean or no applicable boxes, output clean PNG
        if not boxes and has_unwanted:
            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(p),
                detected_watermarks=detected_texts,
                detected_categories=tuple(sorted(found_categories)),
                status="unknown",
                error_message="Unwanted artifacts were detected but no safe bounding boxes were available",
                clean_sha256=compute_sha256(p),
                metadata=dict(detection),
            )

        if not boxes:
            try:
                with Image.open(p) as img:
                    img.save(cleaned_path, format="PNG")
                return CleanedImageResult(
                    asset_id=asset_id,
                    original_path=str(p),
                    cleaned_path=str(cleaned_path),
                    detected_watermarks=(),
                    detected_categories=(),
                    has_cleaned_text=False,
                    clean_sha256=compute_sha256(cleaned_path),
                    status="clean",
                    metadata=detection,
                )
            except Exception:
                return CleanedImageResult(
                    asset_id=asset_id,
                    original_path=str(p),
                    cleaned_path=str(p),
                    detected_watermarks=(),
                    detected_categories=(),
                    has_cleaned_text=False,
                    clean_sha256=compute_sha256(p),
                    status="clean",
                    metadata=detection,
                )

        # 3. Inpaint all detected bounding boxes
        try:
            with Image.open(p) as img:
                img_clean = img.convert("RGB")
                w, h = img_clean.size

                for bbox in boxes:
                    pixel_box = bbox.to_pixel_box(w, h, padding_px=6)
                    _inpaint_box_pil(img_clean, pixel_box)

                img_clean.save(cleaned_path, format="PNG", quality=95)

            clean_sha = compute_sha256(cleaned_path)
            logger.info(
                "Successfully cleaned %d artifacts (categories: %s) from %s -> %s",
                len(boxes),
                list(found_categories),
                p.name,
                cleaned_path.name,
            )

            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(cleaned_path),
                detected_watermarks=detected_texts,
                detected_categories=tuple(sorted(found_categories)),
                has_cleaned_text=True,
                clean_sha256=clean_sha,
                status="cleaned",
                metadata=detection,
            )
        except Exception as exc:
            logger.error("Failed to inpaint image %s: %s", p, exc)
            return CleanedImageResult(
                asset_id=asset_id,
                original_path=str(p),
                cleaned_path=str(p),
                detected_watermarks=detected_texts,
                detected_categories=tuple(sorted(found_categories)),
                has_cleaned_text=False,
                clean_sha256=compute_sha256(p),
                status="failed",
                error_message=str(exc),
                metadata=detection,
            )

    def clean_resource_pack(
        self,
        resource_pack: dict[str, Any],
        output_dir: Path | None = None,
        brand: str = "",
        model: str = "",
    ) -> tuple[dict[str, Any], list[CleanedImageResult]]:
        """Clean all assets in a resource pack and return updated pack copy."""
        pack = dict(resource_pack)
        raw_assets = pack.get("assets", [])
        pack["source_assets"] = [dict(item) for item in raw_assets if isinstance(item, dict)]
        cleaned_assets: list[dict[str, Any]] = []
        results: list[CleanedImageResult] = []

        identity = pack.get("product_identity", {})
        if isinstance(identity, Mapping):
            brand = brand or str(identity.get("brand", ""))
            model = model or str(identity.get("model", ""))

        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id", ""))
            local_path = item.get("local_path") or item.get("path")
            if not local_path:
                cleaned_assets.append(item)
                continue

            res = self.clean_image(
                image_path=local_path,
                asset_id=asset_id,
                output_dir=output_dir,
                brand=brand,
                model=model,
            )
            results.append(res)

            # Update asset dict to point to cleaned_path
            new_item = dict(item)
            source_sha256 = compute_sha256(local_path)
            if res.cleaned_path and res.status in {"clean", "cleaned"}:
                new_item["local_path"] = res.cleaned_path
                new_item["original_path"] = str(local_path)
                new_item["cleaned"] = res.has_cleaned_text
                new_item["detected_watermarks"] = list(res.detected_watermarks)
                new_item["detected_categories"] = list(res.detected_categories)
                new_item["source_sha256"] = source_sha256
                new_item["clean_sha256"] = res.clean_sha256
                new_item["preprocessing"] = {
                    "method": "adaptive_texture_fill_v1",
                    "status": res.status,
                    "source_sha256": source_sha256,
                    "output_sha256": res.clean_sha256,
                    "requires_fresh_image_qc": True,
                }
            elif res.status in {"unknown", "failed"}:
                new_item["preprocessing"] = {
                    "method": "adaptive_texture_fill_v1",
                    "status": res.status,
                    "source_sha256": source_sha256,
                    "requires_fresh_image_qc": True,
                    "error": res.error_message,
                }
            cleaned_assets.append(new_item)

        pack["assets"] = cleaned_assets
        pack["has_preprocessed_images"] = bool(results) and all(
            result.status in {"clean", "cleaned"} for result in results
        )
        pack["preprocessing_requires_fresh_image_qc"] = True
        return pack, results


def format_clean_916_keyframe(
    input_image_path: str | Path,
    output_image_path: str | Path,
    *,
    target_width: int = 720,
    target_height: int = 1280,
    crop_header_ratio: float = 0.18,
    crop_footer_ratio: float = 0.05,
    crop_margin_ratio: float = 0.08,
    background_color: tuple[int, int, int] = (8, 6, 8),
) -> Path:
    """Prepare a pristine 9:16 vertical keyframe from an e-commerce product image.

    Eliminates e-commerce header banners, promotional titles, and marketing borders,
    then centers the isolated product on a high-contrast dark studio canvas.
    """
    in_path = Path(input_image_path).expanduser().resolve()
    out_path = Path(output_image_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(in_path) as im:
        im_rgb = im.convert("RGB")
        w, h = im_rgb.size

        # Compute safe crop box to strip marketing banners and borders
        ymin = int(h * crop_header_ratio)
        ymax = int(h * (1.0 - crop_footer_ratio))
        xmin = int(w * crop_margin_ratio)
        xmax = int(w * (1.0 - crop_margin_ratio))

        # Ensure valid box
        if xmax <= xmin or ymax <= ymin:
            xmin, ymin, xmax, ymax = 0, 0, w, h

        cropped = im_rgb.crop((xmin, ymin, xmax, ymax))

        # Create target 9:16 canvas
        canvas = Image.new("RGB", (target_width, target_height), background_color)

        # Scale product to fit cleanly within canvas margins
        max_content_w = target_width - 40
        max_content_h = target_height - 100
        scale = min(max_content_w / cropped.width, max_content_h / cropped.height)
        new_w = max(1, int(cropped.width * scale))
        new_h = max(1, int(cropped.height * scale))

        resized = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Center product vertically and horizontally
        pos_x = (target_width - new_w) // 2
        pos_y = (target_height - new_h) // 2
        canvas.paste(resized, (pos_x, pos_y))

        canvas.save(str(out_path), quality=98)
        return out_path
