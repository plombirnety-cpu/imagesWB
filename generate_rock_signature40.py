# -*- coding: utf-8 -*-
"""Generate a second 8 x 5 rock series with a distinct canon per band."""
from __future__ import annotations

import argparse
from collections import deque
import json
import shutil
import time
from pathlib import Path

import config
import greenkey_core
import providers
from generate_rock_top40 import SERIES, compose_exact_wordmark, write_json
from PIL import Image


SIGNATURES = {
    "queen": {
        "canon": "operatic British arena rock, regal theatricality, glam-era tailoring, grand piano, sweeping stage fabric and precise showman gestures",
        "palette": "royal crimson, antique gold, ivory, piano black, one restrained cobalt accent",
        "ink": "elegant 1970s concert-poster screenprint with engraved ornamental contours and broad theatrical spotlight shapes",
        "concepts": (
            "a charismatic moustached showman gripping a half microphone stand, arched backwards beneath a crown-shaped fan of stage light; two tiny guitarist silhouettes anchor the base",
            "the full quartet arranged like an operatic proscenium: piano profile at the base, guitarist and drummer in side arches, vocalist stepping through a torn velvet-curtain shape",
            "a white grand piano becoming a vast firebird-like stage shadow while the quartet performs inside its negative spaces; original symbolism, no crest",
            "a kinetic glam-stage tableau with harlequin diamonds, a sweeping cape-like ribbon and one guitarist facing the vocalist across a hard diagonal",
            "an arena finale: raised microphone stand, stacked choir-like silhouettes of all four musicians and a monumental sunburst opening behind them",
        ),
    },
    "metallica": {
        "canon": "1980s Bay Area thrash metal, low-slung angular guitars, hard downstrokes, worn black denim, concrete rehearsal rooms and uncompromising forward momentum",
        "palette": "charcoal black, cold steel, oxidized rust red, dirty bone and one electric white accent",
        "ink": "aggressive hand-inked thrash-zine screenprint with knife-cut shadows, dry-brush edges and large readable masses",
        "concepts": (
            "a four-person thrash lineup lunging forward as one wedge, two angular guitars crossing over a cracked concrete floor and drummer framed by a broken amplifier arch",
            "one commanding rhythm guitarist in a wide stance, down-picking into a fractured steel monolith while three band silhouettes emerge behind the split",
            "a forged wasteland rider made from guitar necks and torn speaker cloth confronting the band; an original metal allegory rather than an album mascot",
            "the quartet performing on a collapsing industrial gantry, cables and stage trusses forming one sharp lightning-shaped negative gap",
            "a rehearsal-room collision: four musicians, battered amplifier wall, flying set lists rendered only as blank paper shapes, and one huge circular cymbal moon",
        ),
    },
    "linkin_park": {
        "canon": "turn-of-the-century alternative metal and electronic hip-hop, emotional dual-vocal contrast, utilitarian streetwear, turntables, photocopy collage and architectural tension",
        "palette": "charcoal, concrete ivory, warning vermilion, deep cobalt and muted silver",
        "ink": "premium hand-built xerox-and-stencil screenprint with controlled torn-paper geometry, brush soundwaves and clean human anatomy",
        "concepts": (
            "two contrasting vocalists back-to-back, one shouting into a wired microphone and one calm at a sampler, joined by a single red waveform over restrained circuit fragments",
            "all six musicians inside an unfinished concrete stairwell, instruments occupying different levels while a turntable cable draws one continuous route through the composition",
            "a lone vocalist breaking through layered blank photocopy sheets and an architectural wall, with the band appearing as small grounded silhouettes below",
            "a producer at turntables and a vocalist at the front edge of a stage, opposed by blue and red blocks that interlock like an unresolved memory",
            "the six-person lineup assembled from clean stencil planes around a monumental circular soundwave aperture; emotional and human, not cyberpunk",
        ),
    },
    "acdc": {
        "canon": "raw Australian high-voltage hard rock, relentless blues riff, giant amplifier stacks, red stage light and a small school-uniform guitarist moving with explosive comic precision",
        "palette": "signal red, warm cream, soot black, brass gold and a small steel-grey accent",
        "ink": "bold 1970s hard-rock gig-poster screenprint with thick brush contour, blunt shadows and high-voltage negative space",
        "concepts": (
            "a small school-uniform guitarist duck-walking down a steep amplifier ramp while the vocalist and rhythm section form a compact wall behind him; one giant lightning fracture",
            "the five-person lineup packed between two towering vintage amplifier stacks, vocalist leaning forward and guitarist airborne over a cable loop",
            "a hand-built stage generator exploding into a red lightning silhouette while the band performs safely inside the open negative center",
            "a giant brass stage bell swinging above a guitarist and singer in opposing poses, with drummer and bass figures stabilizing the triangular base",
            "a locomotive-like wall of speakers driven by the full band, smoke represented only by two broad cream shapes and no atmospheric effects",
        ),
    },
    "korol_i_shut": {
        "canon": "Russian horror punk as a rowdy dark folk tale, two contrasting male storytellers, crooked taverns, jesters, kings, forest lanterns and theatrical grotesque humour",
        "palette": "wine burgundy, old parchment, swamp-night navy, tarnished ochre and black",
        "ink": "hand-carved punk fairytale print mixing lubok directness with rough concert-poster linework, expressive but never glossy",
        "concepts": (
            "two contrasting punk storytellers share one crooked tavern stage while a tiny jester and lantern-bearing king argue in the floor shadows",
            "the band erupts from an open fairytale book represented only by two parchment planes; twisted forest branches become microphone cables",
            "a mischievous jester steals a crown under a huge moon while the two vocalists narrate from opposite sides with the full band grounded below",
            "a crooked village feast turning into a punk concert: fiddler-like guitar pose, drum barrel, jumping crowd silhouettes and one ominous lantern",
            "a theatrical duel between king and fool staged as oversized shadows behind the two storytellers, with rough instruments making a strong triangular base",
        ),
    },
    "kino": {
        "canon": "late-Soviet post-punk austerity, laconic black clothing, night city, rehearsal-room honesty, stark windows, geometric sun and quiet monumental solitude",
        "palette": "coal black, brick red, aged paper, deep Prussian blue and a tiny cold-grey accent",
        "ink": "minimal Soviet-era linocut concert print with broad carved planes, disciplined geometry and large areas of silence",
        "concepts": (
            "a dark-haired vocalist in a plain black shirt stands at a wired microphone before one broken window of light; three bandmates form a quiet horizontal base",
            "the four-person lineup rehearses in a severe concrete room, single hanging lamp, drum kit and two guitars reduced to monumental silhouettes",
            "a lone night-city figure crosses beneath a geometric rising sun while the band appears within four lit window fragments",
            "a vocalist and guitarist face opposite directions across a diagonal tram-wire shape, with low apartment blocks and one red horizon plane",
            "the quartet walks out from a dark stage doorway, instruments in hand, as a restrained blue night and red angular dawn divide the composition",
        ),
    },
    "aria": {
        "canon": "classic Russian heavy metal, soaring long-haired vocalist, twin-guitar harmony, heroic arena staging, winged iron imagery and disciplined 1980s power",
        "palette": "deep crimson, cold silver, warm ivory, coal black and restrained midnight blue",
        "ink": "heroic hand-painted heavy-metal screenprint with crisp steel contours, controlled anatomy and four or five large colour separations",
        "concepts": (
            "a powerful long-haired vocalist raises one hand between two harmonized guitarists while broad silver wings open as stage-light shapes behind the full quintet",
            "the five-person lineup stands on angular arena risers, twin guitars forming a symmetrical V and drummer framed by a single iron halo",
            "an original winged iron guardian bends over a storm-lit fortress while the band performs in the open shield-shaped negative space below",
            "vocalist on a high platform, two guitarists climbing opposing stair diagonals and a torn crimson banner linking the complete lineup",
            "a heroic concert finale with the singer centered, twin guitars crossed low, drummer above and an enormous mechanical wing shadow framing—not enclosing—the group",
        ),
    },
    "piknik": {
        "canon": "Russian art rock as a mysterious mechanical theatre, slender dark-haired musician, handmade surreal instruments, clockwork puppets, crescent forms and elegant unease",
        "palette": "deep violet, oxidized copper, petrol blue, parchment cream and black",
        "ink": "surreal art-nouveau theatre screenprint with precise handmade line, flat jewel-like inks and quiet impossible mechanisms",
        "concepts": (
            "a slender theatrical musician plays an impossible crescent-shaped instrument while two long-limbed puppet shadows operate copper gears behind him",
            "the ensemble performs inside a dismantled clockwork theatre: accordion-like bellows, cello silhouette and hanging moon mechanism arranged asymmetrically",
            "a human figure exchanges a mask with its mechanical shadow across a narrow bridge made from instrument strings, band silhouettes below",
            "a giant articulated stage puppet bows toward the lead musician while violet curtains become gear teeth and one copper moon remains incomplete",
            "the full ensemble in a cabinet of impossible instruments, each member grounded and distinct, with a single elongated shadow threading through open negative space",
        ),
    },
}


COMMON = """
Create a premium vertical ROCK MERCHANDISE ILLUSTRATION designed for high-quality
DTF/screen printing. It must feel specific to the assigned band's musical world,
era and stage character, not like a generic fantasy or generic rock template.

CRAFT STANDARD: confident human-drawn anatomy, believable instruments and hands,
strong silhouette, 4-6 broad spot-colour ink separations, selective fine contour only
where it clarifies a face or instrument, controlled dry-brush imperfection and subtle
analogue registration. Preserve natural adult faces without cosmetic perfection.
The image must read immediately from two metres yet reward a closer look.

ABSOLUTELY FORBIDDEN: photorealism, copied publicity photograph, copied album cover,
official logo, existing mascot, exact cover composition, CGI, 3D bevel, HDR glow,
plastic skin, airbrush, glossy concept art, AI micro-detail, fantasy armour overload,
random sparks, smoke wallpaper, decorative noise everywhere, halftone dots, sticker
outline or clip-art look. Create original merchandise artwork.

EDGE CONTRACT: do not draw any border, frame, rectangular outline, corner line,
panel edge, inner page or perimeter stripe. Every artwork edge must end as an
organic figure, instrument, cloth, light or brush shape against open chroma.

Render directly on one perfectly uniform chroma field. Keep real chroma openings
between separate visual islands and a clean chroma moat around the outer artwork.
No rectangle, poster sheet, card, magazine cover, full-bleed scene, shared backing
blob or white halo.

TEXT CONTRACT: no letters, words, logos, numbers, pseudo-writing, labels or signage.
The exact band title will be typeset locally afterward. Reserve the complete top 21%
as empty uniform chroma with no hair, ray, instrument, ornament or effect inside it.
Every amplifier, drum head, road case and instrument faceplate must be completely
blank: no manufacturer name, brand mark, tiny label or imitation writing.
""".strip()


def prompt_for(slug: str, concept_index: int) -> str:
    signature = SIGNATURES[slug]
    chroma = (
        "saturated digital PURE BLUE RGB(0,0,255) / hex #0000FF, with red channel exactly zero; "
        "never violet, purple, indigo, navy or gradient"
        if slug in {"linkin_park", "piknik"}
        else "saturated digital PURE GREEN RGB(0,255,0) / hex #00FF00"
    )
    return (
        f"{COMMON}\n\n"
        f"BAND-SPECIFIC CANON: {signature['canon']}.\n"
        f"ORIGINAL SCENE: {signature['concepts'][concept_index]}.\n"
        f"PRINTMAKING LANGUAGE: {signature['ink']}.\n"
        f"FIXED INK FAMILY: {signature['palette']}. Avoid any colour dangerously close to the chroma.\n"
        f"BACKGROUND: one perfectly uniform {chroma}, background only."
    )


def build_jobs(pilot: bool) -> list[tuple[str, int]]:
    jobs = [(slug, index) for slug in SIGNATURES for index in range(5)]
    return [(slug, 0) for slug in SIGNATURES] if pilot else jobs


def parse_only_jobs(value: str) -> list[tuple[str, int]]:
    jobs = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        slug, raw_index = item.rsplit(":", 1)
        index = int(raw_index) - 1
        if slug not in SIGNATURES or index not in range(5):
            raise ValueError(f"invalid job {item!r}")
        jobs.append((slug, index))
    return jobs


def _remove_connected_white_edge(rgba: Image.Image) -> Image.Image:
    """Drop only near-white pixels connected to the canvas edge.

    Gemini occasionally adds a thin white paper rim outside the requested chroma.
    Flooding from the boundary removes that rim without harming isolated cream ink.
    """
    image = rgba.copy()
    pixels = image.load()
    width, height = image.size
    seen: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()

    def candidate(x: int, y: int) -> bool:
        red, green, blue, alpha = pixels[x, y]
        return alpha > 0 and red >= 238 and green >= 238 and blue >= 238

    for x in range(width):
        for y in (0, height - 1):
            if candidate(x, y):
                queue.append((x, y))
                seen.add((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if candidate(x, y) and (x, y) not in seen:
                queue.append((x, y))
                seen.add((x, y))

    while queue:
        x, y = queue.popleft()
        red, green, blue, _ = pixels[x, y]
        pixels[x, y] = (red, green, blue, 0)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in seen and candidate(nx, ny):
                seen.add((nx, ny))
                queue.append((nx, ny))
    return image


def process_expected_chroma(path: Path, slug: str) -> str:
    """Key the chroma requested by this series instead of guessing from a stray rim."""
    with Image.open(path) as opened:
        rgb = opened.convert("RGB")
    is_blue = slug in {"linkin_park", "piknik"}
    override = greenkey_core.SCREEN_COLOUR_BLUE if is_blue else greenkey_core.SCREEN_COLOUR_GREEN
    rgba, _, key_code = greenkey_core.process(rgb, override_bg=override, sharp=True)
    rgba = _remove_connected_white_edge(rgba)
    alpha = rgba.getchannel("A")
    alpha = alpha.point(lambda value: 0 if value <= 96 else value)
    rgba.putalpha(alpha)
    if rgba.getchannel("A").getextrema()[0] == 255:
        raise ValueError("forced chroma key produced no transparent pixels")
    temporary = path.with_name(f".{path.name}.signature.tmp")
    rgba.save(temporary, format="PNG")
    temporary.replace(path)
    return "blue" if key_code == 2 else "green"


def generate_one(root: Path, slug: str, concept_index: int, model: str) -> dict:
    group = SERIES[slug]
    stem = f"{slug}_{concept_index + 1:02d}_signature"
    raw_path = root / "raw" / f"{stem}.png"
    final_path = root / "final" / f"{stem}.png"
    prompt_path = root / "prompts" / f"{stem}.txt"
    prompt = prompt_for(slug, concept_index)
    prompt_path.write_text(prompt, encoding="utf-8")
    if final_path.exists():
        return {"group_slug": slug, "concept_index": concept_index + 1, "status": "skipped", "final": str(final_path)}

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
    key = process_expected_chroma(final_path, slug)
    image.close()
    row = {
        "group": group["name"],
        "group_slug": slug,
        "concept_index": concept_index + 1,
        "status": "ok",
        "model": used_model,
        "raw": str(raw_path),
        "final": str(final_path),
        "size": list(size),
        "key": key,
        "seconds": round(time.time() - started, 1),
    }
    print(f"OK {group['name']} / signature {concept_index + 1}: {row['seconds']}s, {size}, key={key}", flush=True)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    root = Path(args.out)
    for child in ("raw", "final", "prompts"):
        (root / child).mkdir(parents=True, exist_ok=True)

    jobs = parse_only_jobs(args.only) if args.only else build_jobs(args.pilot)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    known = {
        (row.get("group_slug"), row.get("concept_index"))
        for row in manifest
        if row.get("status") == "ok" and Path(row.get("final", "")).exists()
    }
    for number, (slug, index) in enumerate(jobs, 1):
        if args.resume and (slug, index + 1) in known:
            print(f"SKIP {slug} {index + 1}", flush=True)
            continue
        print(f"[{number}/{len(jobs)}] {SERIES[slug]['name']} / signature {index + 1}", flush=True)
        try:
            row = generate_one(root, slug, index, config.GEMINI_MODEL_PREMIUM)
        except Exception as exc:
            row = {
                "group": SERIES[slug]["name"], "group_slug": slug,
                "concept_index": index + 1, "status": "error", "error": str(exc),
            }
            print(f"ERROR {SERIES[slug]['name']} / signature {index + 1}: {exc}", flush=True)
        manifest = [old for old in manifest if (old.get("group_slug"), old.get("concept_index")) != (slug, index + 1)]
        manifest.append(row)
        write_json(manifest_path, manifest)


if __name__ == "__main__":
    main()
