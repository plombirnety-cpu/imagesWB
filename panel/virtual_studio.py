# -*- coding: utf-8 -*-
"""Виртуальная фотостудия Print Factory.

Постоянный identity-reference модели + прозрачный принт передаются Gemini как
два независимых изображения. Результат — новый поясной студийный кадр с тем же
лицом, новой позой и принтом на выбранной стороне свободной футболки.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import config
import providers

PANEL_DIR = Path(__file__).resolve().parent
MODELS_PATH = PANEL_DIR / "studio_models.json"
MODELS_DIR = PANEL_DIR / "static" / "studio" / "models"

_LIGHTING_PRESETS = {
    "signature": (
        "Print Factory Signature lighting: a neutral 5000K large-octabox key from "
        "slightly above camera creates clean skin, true shirt color and fully accurate "
        "artwork colors. Add a narrow restrained teal rim from camera-left and a warm "
        "amber rim from camera-right, touching only the outer silhouette, hair, shoulders "
        "and sleeve edges—never tinting the central print area. Keep the subject about one "
        "stop brighter than a deep neutral graphite-to-smoke cyclorama with a soft diffused "
        "oval glow behind the upper torso. Use controlled shadow depth, crisp separation "
        "and premium cotton microtexture. The result is recognizable cinematic e-commerce, "
        "not a nightclub: no neon signs, colored fog, lens flare, blown highlights, hard "
        "color cast, visible spotlight circle or gradient laid over the artwork."
    ),
    "catalog": (
        "Classic catalog lighting: premium soft neutral three-point studio lighting, "
        "large octabox key, gentle neutral rim light, warm light-gray cyclorama, realistic "
        "skin and cotton texture, restrained commercial color grade."
    ),
}

_FRONT_POSES = (
    "relaxed three-quarter stance, one hand resting low near the hip, the other arm relaxed; chest fully unobstructed",
    "confident straight-on stance, shoulders relaxed, both hands below the print area; shirt front fully visible",
    "subtle contrapposto with torso turned about 15 degrees toward camera; arms away from the chest",
    "editorial half-turn with a natural step, hands low and outside the artwork; front print flat and readable",
)
_BACK_POSES = (
    "straight back view with the head turned gently over one shoulder; full back panel unobstructed",
    "three-quarter back pose, shoulders level, arms relaxed low; the center back remains fully visible",
    "editorial back-facing stance with a slight weight shift; hands outside the back artwork",
    "natural walking-away half-step, head turned slightly toward camera; back print large and unobstructed",
)


def load_models() -> list[dict]:
    data = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    return [dict(item) for item in data.get("models", [])]


def get_model(model_id: str) -> dict:
    for model in load_models():
        if model.get("id") == model_id:
            return model
    raise KeyError(model_id)


def public_models() -> list[dict]:
    result = []
    for model in load_models():
        image_name = str(model.get("image") or "")
        result.append({
            "id": model["id"],
            "name": model["name"],
            "gender": model["gender"],
            "description": model.get("description", ""),
            "image_url": f"/static/studio/models/{image_name}",
            "available": bool(image_name) and (MODELS_DIR / image_name).is_file(),
        })
    return result


def _remove_connected_light_background(artwork: Image.Image) -> Image.Image:
    """Убирает только светлый фон, связанный с краями непрозрачного файла."""
    rgba = np.array(artwork.convert("RGBA"), dtype=np.uint8)
    alpha = rgba[..., 3]
    if int(alpha.min()) < 250:
        return artwork

    rgb = rgba[..., :3]
    low = rgb.min(axis=2)
    spread = rgb.max(axis=2) - low
    light = (low >= 238) & (spread <= 20)
    if float(light.mean()) < 0.02:
        return artwork

    _, labels = cv2.connectedComponents(light.astype(np.uint8), connectivity=8)
    border_labels = np.unique(
        np.concatenate((labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]))
    )
    border_labels = border_labels[border_labels != 0]
    if not border_labels.size:
        return artwork

    background = np.isin(labels, border_labels)
    coverage = float(background.mean())
    if coverage < 0.02 or coverage > 0.98:
        return artwork

    new_alpha = alpha.copy()
    new_alpha[background] = 0

    # Мягко убираем нейтральный белёсый fringe возле найденного фона, не
    # затрагивая цветные и внутренние белые детали artwork.
    expanded = cv2.dilate(background.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1).astype(bool)
    fringe = expanded & ~background & (low >= 205) & (spread <= 45)
    fade = np.clip((low.astype(np.float32) - 205.0) / 50.0, 0.0, 1.0)
    fringe_alpha = (255.0 * (1.0 - fade)).astype(np.uint8)
    new_alpha[fringe] = np.minimum(new_alpha[fringe], fringe_alpha[fringe])
    rgba[..., 3] = new_alpha
    return Image.fromarray(rgba, mode="RGBA")


def _load_artwork(path: Path) -> Image.Image:
    with Image.open(path) as source:
        source.load()
        if source.width < 128 or source.height < 128:
            raise ValueError("принт слишком маленький: минимум 128×128")
        if source.width * source.height > 40_000_000:
            raise ValueError("принт слишком большой: максимум 40 мегапикселей")
        artwork = source.convert("RGBA")
    artwork = _remove_connected_light_background(artwork)
    alpha = artwork.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError("принт полностью прозрачный")
    artwork = artwork.crop(bbox)
    if max(artwork.size) > 1800:
        artwork.thumbnail((1800, 1800), Image.LANCZOS)
    return artwork


def _lighting_prompt(lighting: str) -> str:
    try:
        return _LIGHTING_PRESETS[lighting]
    except KeyError as exc:
        raise ValueError("режим освещения должен быть signature или catalog") from exc


def _base_photo_prompt(
    model: dict,
    shirt_color: str,
    placement: str,
    pose_index: int,
    lighting: str,
) -> str:
    color = "deep matte black" if shirt_color == "black" else "clean neutral white"
    side = "front chest" if placement == "front" else "center back"
    poses = _FRONT_POSES if placement == "front" else _BACK_POSES
    pose = poses[pose_index % len(poses)]
    identity = model.get("identity_prompt", "the same adult fashion model")
    lighting_prompt = _lighting_prompt(lighting)
    return f"""Use case: identity-preserve blank-garment product photograph.
Asset type: premium fashion e-commerce base photograph prepared for a later print-transfer edit.
Input image 1 is the immutable identity reference. Use exactly the same fictional adult person: same facial identity, age, skin tone, eyes, hairstyle, hair color and body proportions. Identity description: {identity}.
Wardrobe: a clean blank T-shirt in {color}, heavyweight cotton, crew neck, slightly relaxed oversized fit. Absolutely no print, text, logo, label or graphic anywhere on the shirt.
Garment geometry: preserve genuine cotton drape, seam tension, broad torso curvature, small natural wrinkles and several soft folds across the {side}. Do not iron or flatten the printable panel. Keep the panel unobstructed but visibly three-dimensional so a later edit can follow its real surface.
Pose: {pose}.
Composition: vertical photograph cropped from head to hips or upper thighs, never full body. Keep the T-shirt close, large and sharp. Hands, hair and jewelry stay outside the {side}.
Scene and lighting: {lighting_prompt}
Constraints: one adult person only; photorealistic; anatomically correct hands; face must match image 1; clean blank T-shirt; no watermark; no frame; no mockup UI; no extra objects."""


def _transfer_prompt(shirt_color: str, placement: str) -> str:
    color = "black" if shirt_color == "black" else "white"
    side = "front chest" if placement == "front" else "center back"
    body_surface = (
        "convex chest and ribcage"
        if placement == "front"
        else "shoulder blades and upper-back curvature"
    )
    return f"""Use case: localized photorealistic garment print transfer.
Input image 1 is the immutable base photograph of a person wearing a blank {color} T-shirt. Preserve the person, facial identity, body, pose, hands, hair, garment silhouette, seams, wrinkles, lighting, shadows, background, crop and camera exactly as supplied.
Input image 2 is the exact print artwork. Remove no artwork elements and do not redesign, paraphrase, mirror, crop or invent text. Ignore any transparent or border-connected plain white canvas around the design.
Edit scope: edit only the T-shirt surface at the {side}. Do not regenerate the whole photograph and do not alter pixels outside the garment. Place the complete artwork inside the shirt seams, centered and commercially sized, without bleeding onto skin, sleeves, trousers or background.
Physical transfer: make the ink physically absorbed into the cotton, never pasted on top as a flat Photoshop layer, sticker, rigid poster or floating rectangle. Use the base photograph’s existing fabric luminance as a displacement and shading map. Warp the artwork continuously around the {body_surface}; compress and stretch it locally with perspective, seam tension, wrinkles and folds. Existing folds must remain visible through the ink and must bend the printed lines. Deep creases may softly darken or partially occlude tiny areas exactly as real printed fabric would.
Surface realism: preserve cotton weave and microtexture through every printed color. Reuse the base photo’s highlights, midtones, contact shadows and color temperature inside the print. Edges are absorbed and matte with no halo, drop shadow, outline, glossy decal thickness, rectangular alpha box or perfectly planar geometry.
Priority order: first preserve the base photograph and real garment geometry; second achieve believable cloth integration; third preserve artwork identity and spelling. Return one finished photorealistic e-commerce photograph with no additional text, watermark, frame, UI or objects."""


def render_mockup(
    model_id: str,
    artwork_path: Path,
    shirt_color: str,
    placement: str,
    pose_index: int,
    output_path: Path,
    quality: str = "standard",
    lighting: str = "signature",
) -> Path:
    if shirt_color not in {"black", "white"}:
        raise ValueError("цвет футболки должен быть black или white")
    if placement not in {"front", "back"}:
        raise ValueError("сторона принта должна быть front или back")
    model = get_model(model_id)
    model_path = MODELS_DIR / str(model.get("image") or "")
    if not model_path.is_file():
        raise FileNotFoundError(f"эталон модели {model_id} не найден")

    with Image.open(model_path) as source:
        source.load()
        identity_reference = source.convert("RGB")
    artwork = _load_artwork(Path(artwork_path))
    premium_model = getattr(config, "GEMINI_MODEL_PREMIUM", None) if quality == "premium" else None
    blank_photo = providers.generate_image_with_references(
        _base_photo_prompt(model, shirt_color, placement, pose_index, lighting),
        [identity_reference],
        model=premium_model,
    )
    image = providers.generate_image_with_references(
        _transfer_prompt(shirt_color, placement),
        [blank_photo, artwork],
        model=premium_model,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    return output_path
