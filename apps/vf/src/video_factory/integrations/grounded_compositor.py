"""Enhanced grounded video compositor with authentic product images, dynamic Ken Burns motion, and typography."""
from __future__ import annotations

import math
import os
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def get_default_font(size: int = 32):
    try:
        # Try common Windows TrueType fonts with Vietnamese Unicode support
        font_paths = [
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/tahomabd.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                return ImageFont.truetype(fp, size=size)
    except Exception:
        pass
    return ImageFont.load_default()


def create_gradient_background(width: int, height: int, top_color: tuple, bottom_color: tuple) -> Image.Image:
    """Create a vertical gradient background."""
    base = Image.new("RGB", (width, height), top_color)
    top_r, top_g, top_b = top_color
    bot_r, bot_g, bot_b = bottom_color
    
    # Fast numpy gradient
    y = np.linspace(0, 1, height)[:, None]
    r = np.uint8(top_r + y * (bot_r - top_r))
    g = np.uint8(top_g + y * (bot_g - top_g))
    b = np.uint8(top_b + y * (bot_b - top_b))
    rgb = np.dstack([np.repeat(r, width, axis=1), np.repeat(g, width, axis=1), np.repeat(b, width, axis=1)])
    return Image.fromarray(rgb, "RGB")


def compose_product_frame(
    product_img: Image.Image,
    bg_gradient: tuple[tuple, tuple],
    badge_title: str,
    headline_text: str,
    sub_text: str,
    scale_factor: float = 1.0,
    pan_offset_y: float = 0.0,
    light_phase: float = 0.0,
    canvas_w: int = 720,
    canvas_h: int = 1280,
) -> Image.Image:
    """Compose a single high-quality video frame with the authentic product image and overlays."""
    top_col, bot_col = bg_gradient
    bg = create_gradient_background(canvas_w, canvas_h, top_col, bot_col)
    
    # 1. Subtle radial spotlight behind product
    spotlight_radius = int(canvas_w * 0.45 * scale_factor)
    spotlight = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sp_draw = ImageDraw.Draw(spotlight)
    center_x = canvas_w // 2
    center_y = int(canvas_h * 0.48 + pan_offset_y)
    
    pulse_alpha = int(45 + 15 * math.sin(light_phase * math.pi * 2))
    sp_draw.ellipse(
        [
            center_x - spotlight_radius,
            center_y - spotlight_radius,
            center_x + spotlight_radius,
            center_y + spotlight_radius,
        ],
        fill=(255, 255, 255, pulse_alpha),
    )
    spotlight = spotlight.filter(ImageFilter.GaussianBlur(radius=60))
    bg.paste(spotlight, (0, 0), spotlight)

    # 2. Process and scale Product Image
    p_img = product_img.convert("RGBA")
    max_p_w = int(canvas_w * 0.78 * scale_factor)
    max_p_h = int(canvas_h * 0.48 * scale_factor)
    
    p_w, p_h = p_img.size
    scale = min(max_p_w / p_w, max_p_h / p_h)
    new_w = max(10, int(p_w * scale))
    new_h = max(10, int(p_h * scale))
    p_resized = p_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Apply smooth rounded corners to product image card
    mask = Image.new("L", (new_w, new_h), 0)
    m_draw = ImageDraw.Draw(mask)
    m_draw.rounded_rectangle([0, 0, new_w, new_h], radius=24, fill=255)
    
    if "A" in p_resized.getbands():
        orig_alpha = p_resized.split()[3]
        combined_alpha = Image.composite(orig_alpha, Image.new("L", (new_w, new_h), 0), mask)
        p_resized.putalpha(combined_alpha)
    else:
        p_resized.putalpha(mask)

    # Add soft drop shadow
    shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(shadow)
    sh_x = (canvas_w - new_w) // 2
    sh_y = int(canvas_h * 0.48 - new_h // 2 + pan_offset_y + 12)
    
    # Ellipse shadow base
    sh_draw.ellipse(
        [sh_x + new_w * 0.15, sh_y + new_h * 0.85, sh_x + new_w * 0.85, sh_y + new_h + 20],
        fill=(0, 0, 0, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=25))
    bg.paste(shadow, (0, 0), shadow)

    # Paste product
    p_x = (canvas_w - new_w) // 2
    p_y = int(canvas_h * 0.48 - new_h // 2 + pan_offset_y)
    bg.paste(p_resized, (p_x, p_y), p_resized)

    # 3. Typography & Badges Overlay
    draw = ImageDraw.Draw(bg)
    font_badge = get_default_font(24)
    font_head = get_default_font(38)
    font_sub = get_default_font(28)

    # Top Tag Badge
    badge_w = int(len(badge_title) * 16 + 40)
    badge_h = 44
    badge_x = (canvas_w - badge_w) // 2
    badge_y = 90
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=22,
        fill=(235, 75, 40),
    )
    draw.text((badge_x + 20, badge_y + 8), badge_title.upper(), fill=(255, 255, 255), font=font_badge)

    # Bottom Content Card
    card_margin = 40
    card_y = int(canvas_h * 0.76)
    card_h = int(canvas_h * 0.18)
    
    # Glassmorphism Card
    glass = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glass)
    g_draw.rounded_rectangle(
        [card_margin, card_y, canvas_w - card_margin, card_y + card_h],
        radius=24,
        fill=(15, 20, 30, 200),
        outline=(255, 255, 255, 50),
        width=2,
    )
    bg.paste(glass, (0, 0), glass)

    # Draw Headlines inside card
    draw.text((card_margin + 24, card_y + 24), headline_text, fill=(255, 220, 90), font=font_head)
    
    # Multi-line or single-line subtext
    if len(sub_text) > 40:
        words = sub_text.split()
        mid = len(words) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])
        draw.text((card_margin + 24, card_y + 80), line1, fill=(230, 235, 245), font=font_sub)
        draw.text((card_margin + 24, card_y + 118), line2, fill=(200, 210, 225), font=font_sub)
    else:
        draw.text((card_margin + 24, card_y + 85), sub_text, fill=(230, 235, 245), font=font_sub)

    return bg


def render_animated_scene_clip(
    output_path: Path,
    product_img: Image.Image,
    bg_gradient: tuple[tuple, tuple],
    badge_title: str,
    headline_text: str,
    sub_text: str,
    duration_seconds: float = 6.0,
    fps: int = 24,
    motion_type: str = "zoom_in",
    width: int = 720,
    height: int = 1280,
) -> Path:
    """Render a dynamic 24fps MP4 clip with authentic product image and Ken Burns motion."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_seconds * fps)
    
    container = av.open(str(output_path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": "20", "preset": "medium"}

    for i in range(frames):
        t = i / max(1, frames - 1)  # 0.0 -> 1.0
        
        # Motion parameters
        if motion_type == "zoom_in":
            scale = 0.95 + 0.15 * t
            pan_y = 10.0 * (1.0 - t)
        elif motion_type == "zoom_out":
            scale = 1.10 - 0.12 * t
            pan_y = -8.0 * t
        elif motion_type == "pan_up":
            scale = 1.02 + 0.04 * math.sin(t * math.pi)
            pan_y = 18.0 - 36.0 * t
        else:
            scale = 1.0 + 0.06 * math.sin(t * math.pi)
            pan_y = 6.0 * math.cos(t * math.pi)

        frame_pil = compose_product_frame(
            product_img=product_img,
            bg_gradient=bg_gradient,
            badge_title=badge_title,
            headline_text=headline_text,
            sub_text=sub_text,
            scale_factor=scale,
            pan_offset_y=pan_y,
            light_phase=t,
            canvas_w=width,
            canvas_h=height,
        )

        frame_av = av.VideoFrame.from_image(frame_pil)
        frame_av.pts = i
        frame_av.time_base = Fraction(1, fps)
        for packet in stream.encode(frame_av):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return output_path
