from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


PRESET_SIZES = (24, 48, 72, 96, 120, 144, 221)


@dataclass(frozen=True)
class PaletteColour:
    code: str
    rgb: tuple[int, int, int]
    ordinal: int


@dataclass(frozen=True)
class PaletteRevision:
    palette_id: str
    colours: Mapping[str, PaletteColour]
    presets: Mapping[int, tuple[str, ...]]
    digest: str


def _rgb_from_hex(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) != 6:
        raise ValueError("palette hex colour must contain six hexadecimal digits")
    try:
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError as error:
        raise ValueError("palette hex colour must contain six hexadecimal digits") from error


def _load_colours(series: object) -> dict[str, PaletteColour]:
    if not isinstance(series, dict) or not series:
        raise ValueError("palette series must be a non-empty object")
    colours: dict[str, PaletteColour] = {}
    ordinal = 0
    for prefix, values in series.items():
        if not isinstance(prefix, str) or len(prefix) != 1 or not prefix.isupper():
            raise ValueError("palette series keys must be one uppercase letter")
        if not isinstance(values, list) or not values:
            raise ValueError("palette series values must be non-empty lists")
        for number, value in enumerate(values, start=1):
            code = f"{prefix}{number}"
            colours[code] = PaletteColour(code, _rgb_from_hex(value), ordinal)
            ordinal += 1
    return colours


def _load_presets(raw_presets: object, colours: Mapping[str, PaletteColour]) -> dict[int, tuple[str, ...]]:
    if not isinstance(raw_presets, dict):
        raise ValueError("palette presets must be an object")
    presets: dict[int, tuple[str, ...]] = {}
    for size in PRESET_SIZES[:-1]:
        values = raw_presets.get(str(size))
        if not isinstance(values, list) or len(values) != size:
            raise ValueError(f"palette preset {size} must contain exactly {size} codes")
        if not all(isinstance(code, str) and code in colours for code in values):
            raise ValueError(f"palette preset {size} contains an unknown code")
        if len(set(values)) != len(values):
            raise ValueError(f"palette preset {size} contains duplicate codes")
        presets[size] = tuple(values)
    presets[221] = tuple(colours)
    for smaller, larger in zip(PRESET_SIZES, PRESET_SIZES[1:]):
        if not set(presets[smaller]) < set(presets[larger]):
            raise ValueError(f"palette preset {smaller} must be a strict subset of {larger}")
    return presets


def load_palette(path: Path) -> PaletteRevision:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("palette schema_version must equal 1")
    palette_id = raw.get("palette_id")
    if not isinstance(palette_id, str) or not palette_id:
        raise ValueError("palette_id must be a non-empty string")
    colours = _load_colours(raw.get("series"))
    if len(colours) != 221:
        raise ValueError("a-m-v1 must contain exactly 221 colours")
    presets = _load_presets(raw.get("presets"), colours)
    canonical = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return PaletteRevision(
        palette_id=palette_id,
        colours=MappingProxyType(colours),
        presets=MappingProxyType(presets),
        digest=sha256(canonical.encode("utf-8")).hexdigest(),
    )


def nearest_colour(
    palette: PaletteRevision, rgb: tuple[int, int, int], *, preset_size: int
) -> PaletteColour:
    if preset_size not in palette.presets:
        raise ValueError(f"unsupported palette preset: {preset_size}")
    if len(rgb) != 3 or any(not isinstance(value, int) or not 0 <= value <= 255 for value in rgb):
        raise ValueError("rgb must contain three integers between 0 and 255")
    return min(
        (palette.colours[code] for code in palette.presets[preset_size]),
        key=lambda colour: (
            sum((channel - target) ** 2 for channel, target in zip(colour.rgb, rgb)),
            colour.ordinal,
        ),
    )
