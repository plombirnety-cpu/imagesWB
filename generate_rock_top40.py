# -*- coding: utf-8 -*-
"""Generate the approved 8 x 5 rock-print series through Gemini Nano Banana."""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import config
import providers
from greenkey_postprocess import process_file
from PIL import Image, ImageDraw, ImageFont


SERIES = {
    "queen": {
        "name": "Queen",
        "subject": "a theatrical British arena-rock quartet with one charismatic moustached vocalist, operatic stage movement and elegant 1970s silhouettes; original likenesses, not portraits",
        "myth": "an original operatic stage apparition built from a crown, sweeping fabric and a rising firebird silhouette; do not copy the official crest",
        "palette": "crimson, antique gold, charcoal and warm bone",
        "title_fill": "#B9212B",
        "font": "Cinzel[wght].ttf",
    },
    "metallica": {
        "name": "Metallica",
        "subject": "an original four-person 1980s Bay Area thrash-metal lineup with angular guitars, uncompromising stance, worn black denim and no resemblance to any exact photograph",
        "myth": "an original forged-metal wasteland rider confronting a cracked monolith; no existing album mascot or cover scene",
        "palette": "rust red, cold steel blue, charcoal and warm bone",
        "title_fill": "#B6402E",
        "font": "Anton-Regular.ttf",
    },
    "linkin_park": {
        "name": "Linkin Park",
        "subject": "an original six-person turn-of-the-century alternative-metal lineup built around the contrast of an intense vocalist and a calm rapper-producer, with restrained utilitarian stage clothes",
        "myth": "an original human silhouette breaking through layered memory fragments, torn circuitry and one brush-painted sound wave",
        "palette": "cobalt, vermilion, charcoal and warm bone",
        "title_fill": "#D84835",
        "font": "ArchivoBlack-Regular.ttf",
    },
    "acdc": {
        "name": "AC/DC",
        "subject": "an original five-person high-voltage Australian hard-rock lineup led by a small kinetic school-uniform guitarist silhouette; do not recreate a real photograph or exact face",
        "myth": "an original high-voltage stage generator splitting a dark storm cloud with one huge hand-painted lightning fracture; no copied album cover",
        "palette": "signal red, antique gold, charcoal and warm bone",
        "title_fill": "#D32B26",
        "font": "Anton-Regular.ttf",
    },
    "korol_i_shut": {
        "name": "Король и Шут",
        "subject": "an original Russian horror-punk ensemble with two contrasting male storytellers, jagged punk silhouettes and a dark theatrical fairytale mood; no exact portrait likeness",
        "myth": "an original Russian punk-fairytale confrontation between a crooked forest jester and a lantern-bearing king, theatrical rather than photorealistic",
        "palette": "burgundy, ochre, midnight blue and warm bone",
        "title_fill": "#8F2345",
        "font": "FreeSansBold.ttf",
    },
    "kino": {
        "name": "КИНО",
        "subject": "an original austere four-person late-Soviet post-punk lineup led by a dark-haired vocalist in a simple black shirt, laconic and monumental rather than portrait-like",
        "myth": "an original austere night-city allegory: a lone figure, one broken window of light and a rising geometric sun; do not copy an album cover",
        "palette": "brick red, deep cobalt, charcoal and warm bone",
        "title_fill": "#BB352D",
        "font": "FreeSansBold.ttf",
    },
    "aria": {
        "name": "АРИЯ",
        "subject": "an original classic Russian heavy-metal quintet with a powerful long-haired vocalist, twin-guitar geometry and theatrical 1980s stage silhouettes",
        "myth": "an original winged iron guardian above a storm-bent fortress banner, simplified into two powerful silhouettes; no official mascot or album scene",
        "palette": "crimson, cold silver, charcoal and warm bone",
        "title_fill": "#B9232E",
        "font": "FreeSansBold.ttf",
    },
    "piknik": {
        "name": "ПИКНИК",
        "subject": "an original Russian art-rock ensemble led by a slender dark-haired theatrical musician, with surreal handmade instruments and mysterious stage mechanisms",
        "myth": "an original surreal theatre mechanism: a long-limbed puppet musician, crescent instrument and impossible clockwork shadow",
        "palette": "deep violet, copper, petrol blue and warm bone",
        "title_fill": "#8B4D9F",
        "font": "FreeSansBold.ttf",
    },
}


VARIANTS = (
    (
        "frontman",
        "FRONTMAN IMPACT: one iconic lead performer, waist-up or three-quarter view, "
        "one real instrument or microphone cable forming a single strong diagonal, "
        "with one incomplete stage-light disc and a few broad broken rays.",
    ),
    (
        "lineup",
        "COLOURIZED LINEUP COLLAGE: the complete recognizable lineup shares one candid "
        "rehearsal or backstage interaction. Treat every member as a real cut-paper "
        "photo silkscreen in a different flat colour channel. Count people correctly; "
        "no duplicated person.",
    ),
    (
        "myth",
        "METAL OR THEATRICAL MYTH: build the group-specific original allegory described "
        "below as a hand-painted 1980s screenprint using only two large interacting "
        "silhouettes over an incomplete sun or moon disc.",
    ),
    (
        "stage_symbol",
        "STAGE SYMBOL: show one performer in a quieter iconic pose integrated with one "
        "large group-specific stage object, instrument or theatrical mechanism. Use "
        "asymmetrical negative space and no circular badge enclosure.",
    ),
    (
        "hybrid",
        "HUMAN + MYTH HYBRID: one dominant performer in front, two smaller bandmates "
        "behind and the group-specific original allegory appearing only as a bold "
        "secondary shadow. One instrument forms the compositional spine.",
    ),
)


COMMON = """
Create a premium vertical ROCK BAND APPAREL PRINT, not a poster and not a photograph.
HUMAN PRINTMAKER RULE: the result must look hand-separated and screen-printed by a
human illustrator. Use exactly 3-5 broad flat ink masses, decisive hand-inked contour,
only a few deliberate shadow shapes, quiet surfaces and slightly imperfect analogue
registration. Faces must stay natural, specific and unretouched. It must read from two
metres. Absolutely no photorealism, CGI, 3D bevels, HDR light, glossy concept art,
airbrushed skin, pores everywhere, hyper-sharp microtexture, ornate noise on every
surface, generic fantasy armour, random sparks, smoke wallpaper or swirling-effect
overload. NO HALFTONE DOT PATTERN.

The artwork must be original merchandise art, never a reconstruction of an album
cover, publicity photograph, official logo or existing mascot artwork. Preserve
recognizable people through economical facial landmarks and silhouette, not through
photo rendering. Keep anatomy, hands, instruments and member count correct.

Render the artwork directly on one perfectly uniform chroma-key field. Keep genuine
chroma gaps between separated islands and a completely empty 7% chroma moat on all
four sides. No rectangular page, poster sheet, card, magazine cover, full-bleed scene,
sticker cutline, white halo, shared backing or enclosing blob.

TEXT CONTRACT: render absolutely no letters, words, logos, numbers, pseudo-letters,
signage or text-like marks anywhere. A deterministic real-font wordmark will be added
after generation. Reserve the entire top 22% as completely empty, uniform chroma;
no head, hair, instrument, ray, disc, effect or artwork may enter that title zone.
""".strip()


def prompt_for(group: dict, variant_index: int) -> str:
    variant_name, variant = VARIANTS[variant_index]
    bg = "uniform pure BLUE chroma #0000FF" if variant_name == "lineup" else "uniform pure GREEN chroma #00FF00"
    return (
        f"{COMMON}\n\nVISUAL SUBJECT: {group['subject']}.\n"
        f"ASSIGNED COMPOSITION: {variant}\n"
        f"GROUP-SPECIFIC ALLEGORY: {group['myth']}.\n"
        f"REAL-INK PALETTE: {group['palette']}; broad masses only.\n"
        f"BACKGROUND: {bg}. The chroma colour is background only and must not appear in artwork."
    )


def build_jobs(pilot: bool) -> list[tuple[str, int]]:
    jobs = [(slug, i) for slug in SERIES for i in range(len(VARIANTS))]
    return [("queen", 0), ("korol_i_shut", 1), ("metallica", 2)] if pilot else jobs


def parse_only_jobs(value: str) -> list[tuple[str, int]]:
    jobs = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        slug, raw_index = item.rsplit(":", 1)
        index = int(raw_index) - 1
        if slug not in SERIES or index not in range(len(VARIANTS)):
            raise ValueError(f"invalid job {item!r}")
        jobs.append((slug, index))
    return jobs


def write_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _font_for(group: dict, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(Path(__file__).parent / "fonts" / group["font"], size)
    if "[wght]" in group["font"]:
        try:
            font.set_variation_by_axes([900])
        except Exception:
            pass
    return font


def _title_lines(name: str) -> list[str]:
    return ["КОРОЛЬ", "И ШУТ"] if name == "Король и Шут" else [name.upper()]


def compose_exact_wordmark(image: Image.Image, group: dict) -> Image.Image:
    """Add exact band title locally; Gemini never renders letters for this series."""
    rgb = image.convert("RGB")
    draw = ImageDraw.Draw(rgb)
    width, height = rgb.size
    lines = _title_lines(group["name"])
    max_width = int(width * 0.90)
    zone_height = int(height * 0.205)
    gap = max(2, int(height * 0.006))
    font_size = int(height * (0.105 if len(lines) == 1 else 0.073))
    while font_size >= 24:
        font = _font_for(group, font_size)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        widths = [box[2] - box[0] for box in boxes]
        heights = [box[3] - box[1] for box in boxes]
        if max(widths) <= max_width and sum(heights) + gap * (len(lines) - 1) <= zone_height:
            break
        font_size -= 2
    total_height = sum(heights) + gap * (len(lines) - 1)
    y = max(int(height * 0.018), (zone_height - total_height) // 2)
    outer = max(3, font_size // 13)
    inner = max(2, font_size // 28)
    for line, box, line_width, line_height in zip(lines, boxes, widths, heights):
        x = (width - line_width) // 2 - box[0]
        line_y = y - box[1]
        draw.text((x + 3, line_y + 5), line, font=font, fill="#1B1B1B", stroke_width=outer + 2, stroke_fill="#1B1B1B")
        draw.text((x, line_y), line, font=font, fill="#F0DFB9", stroke_width=outer, stroke_fill="#F0DFB9")
        draw.text((x, line_y), line, font=font, fill=group["title_fill"], stroke_width=inner, stroke_fill="#202124")
        y += line_height + gap
    return rgb


def generate_one(root: Path, group_slug: str, variant_index: int, model: str) -> dict:
    group = SERIES[group_slug]
    variant_slug = VARIANTS[variant_index][0]
    stem = f"{group_slug}_{variant_index + 1:02d}_{variant_slug}"
    raw_path = root / "raw" / f"{stem}.png"
    final_path = root / "final" / f"{stem}.png"
    prompt_path = root / "prompts" / f"{stem}.txt"
    prompt = prompt_for(group, variant_index)
    prompt_path.write_text(prompt, encoding="utf-8")
    if final_path.exists():
        return {"group": group["name"], "variant": variant_slug, "status": "skipped", "final": str(final_path)}

    started = time.time()
    used_model = model
    try:
        image = providers.generate_image(prompt, model=model)
    except Exception as exc:
        if model == config.GEMINI_MODEL:
            raise
        used_model = config.GEMINI_MODEL
        print(f"  premium failed, fallback {used_model}: {exc}", flush=True)
        image = providers.generate_image(prompt, model=used_model)

    image = compose_exact_wordmark(image, group)
    size = image.size
    image.save(raw_path, format="PNG")
    shutil.copy2(raw_path, final_path)
    key = process_file(final_path, sharp=True)
    image.close()
    result = {
        "group": group["name"],
        "group_slug": group_slug,
        "variant": variant_slug,
        "variant_index": variant_index + 1,
        "status": "ok",
        "model": used_model,
        "raw": str(raw_path),
        "final": str(final_path),
        "size": list(size),
        "key": key.key,
        "detected_bg": list(key.detected_bg),
        "seconds": round(time.time() - started, 1),
    }
    print(f"OK {group['name']} / {variant_slug}: {result['seconds']}s, {size}, key={key.key}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--pilot", action="store_true", help="Generate three cross-style pilots only")
    parser.add_argument("--resume", action="store_true", help="Skip completed finals")
    parser.add_argument("--only", default="", help="Comma-separated slug:1-based-variant jobs")
    args = parser.parse_args()
    root = Path(args.out)
    for child in ("raw", "final", "prompts"):
        (root / child).mkdir(parents=True, exist_ok=True)

    model = config.GEMINI_MODEL_PREMIUM
    jobs = parse_only_jobs(args.only) if args.only else build_jobs(args.pilot)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    known = {(row.get("group_slug"), row.get("variant_index")) for row in manifest if row.get("status") == "ok"}
    for number, (slug, variant_index) in enumerate(jobs, 1):
        if args.resume and (slug, variant_index + 1) in known:
            print(f"SKIP {slug} {variant_index + 1}", flush=True)
            continue
        print(f"[{number}/{len(jobs)}] {SERIES[slug]['name']} / {VARIANTS[variant_index][0]}", flush=True)
        try:
            row = generate_one(root, slug, variant_index, model)
        except Exception as exc:  # keep the paid batch resumable after partial failure
            row = {
                "group": SERIES[slug]["name"],
                "group_slug": slug,
                "variant": VARIANTS[variant_index][0],
                "variant_index": variant_index + 1,
                "status": "error",
                "error": str(exc),
            }
            print(f"ERROR {row['group']} / {row['variant']}: {exc}", flush=True)
        manifest = [old for old in manifest if (old.get("group_slug"), old.get("variant_index")) != (slug, variant_index + 1)]
        manifest.append(row)
        write_json(manifest_path, manifest)


if __name__ == "__main__":
    main()
