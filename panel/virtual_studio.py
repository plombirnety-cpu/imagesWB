# -*- coding: utf-8 -*-
"""Виртуальная фотостудия Print Factory.

Постоянный identity-reference модели + прозрачный принт передаются Gemini как
два независимых изображения. Результат — новый поясной студийный кадр с тем же
лицом, новой позой и принтом на выбранной стороне свободной футболки.
"""
from __future__ import annotations

import json
from pathlib import Path

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


def _load_artwork(path: Path) -> Image.Image:
    with Image.open(path) as source:
        source.load()
        if source.width < 128 or source.height < 128:
            raise ValueError("принт слишком маленький: минимум 128×128")
        if source.width * source.height > 40_000_000:
            raise ValueError("принт слишком большой: максимум 40 мегапикселей")
        artwork = source.convert("RGBA")
    alpha = artwork.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError("принт полностью прозрачный")
    artwork = artwork.crop(bbox)
    if max(artwork.size) > 1800:
        artwork.thumbnail((1800, 1800), Image.LANCZOS)
    return artwork


def _prompt(
    model: dict,
    shirt_color: str,
    placement: str,
    pose_index: int,
    lighting: str = "signature",
) -> str:
    color = "deep matte black" if shirt_color == "black" else "clean neutral white"
    side = "front chest" if placement == "front" else "center back"
    body_surface = (
        "the natural convex chest and ribcage"
        if placement == "front"
        else "the natural shoulder-blade and upper-back curvature"
    )
    poses = _FRONT_POSES if placement == "front" else _BACK_POSES
    pose = poses[pose_index % len(poses)]
    identity = model.get("identity_prompt", "the same adult fashion model")
    try:
        lighting_prompt = _LIGHTING_PRESETS[lighting]
    except KeyError as exc:
        raise ValueError("режим освещения должен быть signature или catalog") from exc
    return f"""Use case: identity-preserve product-mockup.
Asset type: premium fashion e-commerce photograph for a T-shirt print listing.
Input image 1 is the immutable identity reference. Use exactly the same fictional adult person: same facial identity, age, skin tone, eyes, hairstyle, hair color and body proportions. Identity description: {identity}.
Input image 2 is the exact print artwork. Reproduce that artwork faithfully on the {side}: preserve its composition, Cyrillic spelling, colors, linework and proportions. Do not redesign, paraphrase, crop, mirror or invent any part of the artwork.
Wardrobe: a plain {color} heavyweight cotton crew-neck T-shirt with a slightly relaxed oversized fit, natural sleeves and realistic fabric folds. No other logos, labels or graphics. The print belongs only on the {side}.
Fabric integration: the artwork is physically printed into the cotton, never pasted on top as a flat sticker, rigid poster or floating layer. Conform the whole artwork continuously to {body_surface}. Apply realistic local perspective and gentle foreshortening from the camera angle. Let the artwork bend smoothly over broad cloth curvature and deform subtly with natural tension, wrinkles and folds while keeping every supplied element recognizable and correctly ordered. Cotton weave, soft highlights and garment shadows must remain visible through the ink; print brightness and contrast must respond to the same studio light as the shirt. Preserve clean adhered edges with no halo, border, rectangular alpha box, drop shadow or raised sticker thickness. Folds may pass naturally through the print, but must not destroy spelling, faces or essential details.
Pose: {pose}.
Composition: vertical photograph cropped from head to hips or upper thighs, never full body. Keep the T-shirt and the entire print close, large, sharp and easy to inspect. Do not let hands, hair or jewelry hide important parts of the print.
Scene and lighting: {lighting_prompt}
Constraints: one adult person only; photorealistic; anatomically correct hands; the face must match image 1; the artwork must match image 2; the print must follow the garment surface rather than remain geometrically flat; no text outside the supplied artwork; no watermark; no frame; no mockup UI; no extra objects."""


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
    image = providers.generate_image_with_references(
        _prompt(model, shirt_color, placement, pose_index, lighting),
        [identity_reference, artwork],
        model=premium_model,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    return output_path
