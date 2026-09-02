from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Mapping
import warnings

from PIL import Image, ImageCms, ImageOps

from palette import PaletteRevision, nearest_colour


STANDARD_BOARDS = {"52": (52, 52), "78": (78, 78), "104": (104, 104)}
MAX_SOURCE_PIXELS = 40_000_000
MAX_GRID_CELLS = 20_000
ALPHA_EMPTY_THRESHOLD = 128
SUPPORTED_FORMATS = {"JPEG", "PNG"}


@dataclass(frozen=True)
class Candidate:
    palette_id: str
    palette_digest: str
    preset_size: int
    columns: int
    rows: int
    board: str | None
    background_code: str | None
    source_sha256: str
    source_size: tuple[int, int]
    matrix: tuple[tuple[str | None, ...], ...]
    counts: Mapping[str, int]
    empty_cells: int

    def to_dict(self) -> dict[str, object]:
        return {
            "palette_id": self.palette_id,
            "palette_digest": self.palette_digest,
            "preset_size": self.preset_size,
            "columns": self.columns,
            "rows": self.rows,
            "board": self.board,
            "background_code": self.background_code,
            "source_sha256": self.source_sha256,
            "source_size": list(self.source_size),
            "matrix": [list(row) for row in self.matrix],
            "counts": dict(self.counts),
            "empty_cells": self.empty_cells,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "Candidate":
        matrix = raw.get("matrix")
        source_size = raw.get("source_size")
        counts = raw.get("counts")
        if not isinstance(matrix, list) or not isinstance(source_size, list):
            raise ValueError("candidate data is missing a matrix or source size")
        if len(source_size) != 2 or not all(isinstance(value, int) for value in source_size):
            raise ValueError("candidate source size is invalid")
        if not isinstance(counts, dict) or not all(
            isinstance(code, str) and isinstance(count, int)
            for code, count in counts.items()
        ):
            raise ValueError("candidate counts are invalid")
        frozen_matrix = tuple(
            tuple(cell if isinstance(cell, str) else None for cell in row)
            for row in matrix
            if isinstance(row, list)
        )
        if len(frozen_matrix) != len(matrix):
            raise ValueError("candidate matrix rows are invalid")
        required_strings = ("palette_id", "palette_digest", "source_sha256")
        if not all(isinstance(raw.get(field), str) for field in required_strings):
            raise ValueError("candidate identifiers are invalid")
        numeric_fields = ("preset_size", "columns", "rows", "empty_cells")
        if not all(isinstance(raw.get(field), int) for field in numeric_fields):
            raise ValueError("candidate numeric fields are invalid")
        board = raw.get("board")
        background_code = raw.get("background_code")
        if board is not None and not isinstance(board, str):
            raise ValueError("candidate board is invalid")
        if background_code is not None and not isinstance(background_code, str):
            raise ValueError("candidate background code is invalid")
        return cls(
            palette_id=raw["palette_id"],
            palette_digest=raw["palette_digest"],
            preset_size=raw["preset_size"],
            columns=raw["columns"],
            rows=raw["rows"],
            board=board,
            background_code=background_code,
            source_sha256=raw["source_sha256"],
            source_size=(source_size[0], source_size[1]),
            matrix=frozen_matrix,
            counts=dict(sorted(counts.items())),
            empty_cells=raw["empty_cells"],
        )


def _source_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_grid(columns: int, rows: int, board: str | None) -> None:
    if not isinstance(columns, int) or not isinstance(rows, int) or columns < 1 or rows < 1:
        raise ValueError("columns and rows must be positive integers")
    if columns * rows > MAX_GRID_CELLS:
        raise ValueError(f"grid exceeds the {MAX_GRID_CELLS} cell limit")
    if board is None:
        return
    if board not in STANDARD_BOARDS:
        raise ValueError("board must be one of 52, 78, or 104")
    limit_columns, limit_rows = STANDARD_BOARDS[board]
    if columns > limit_columns or rows > limit_rows:
        raise ValueError(f"grid exceeds board {board} limit {limit_columns}x{limit_rows}")


def _decode_source(source: Path) -> tuple[Image.Image, tuple[int, int]]:
    if not source.is_file():
        raise ValueError("source image must be a readable file")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(source) as probe:
                if probe.format not in SUPPORTED_FORMATS:
                    raise ValueError("source image must be JPG or PNG")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValueError("animated images are not supported")
                original_size = probe.size
                if original_size[0] * original_size[1] > MAX_SOURCE_PIXELS:
                    raise ValueError(f"source image exceeds {MAX_SOURCE_PIXELS} pixels")
                probe.verify()
            with Image.open(source) as decoded:
                image = ImageOps.exif_transpose(decoded)
                profile = image.info.get("icc_profile")
                if profile:
                    try:
                        image = ImageCms.profileToProfile(
                            image,
                            ImageCms.ImageCmsProfile(BytesIO(profile)),
                            ImageCms.createProfile("sRGB"),
                            outputMode="RGBA",
                        )
                    except ImageCms.PyCMSError as error:
                        raise ValueError("source image ICC profile is invalid") from error
                else:
                    image = image.convert("RGBA")
                return image.copy(), original_size
        except (Image.UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError("source image is invalid or unsafe") from error


def _contain(image: Image.Image, columns: int, rows: int) -> tuple[Image.Image, int, int]:
    scale = min(columns / image.width, rows / image.height)
    width = max(1, min(columns, round(image.width * scale)))
    height = max(1, min(rows, round(image.height * scale)))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    return resized, (columns - width) // 2, (rows - height) // 2


def build_candidate(
    source: Path,
    palette: PaletteRevision,
    *,
    preset_size: int,
    columns: int,
    rows: int,
    board: str | None,
    background_code: str | None = None,
) -> Candidate:
    _validate_grid(columns, rows, board)
    if preset_size not in palette.presets:
        raise ValueError(f"unsupported palette preset: {preset_size}")
    if background_code is not None and background_code not in palette.presets[preset_size]:
        raise ValueError("background code must be in the selected palette preset")
    image, source_size = _decode_source(Path(source))
    resized, offset_x, offset_y = _contain(image, columns, rows)
    matrix: list[list[str | None]] = [[None for _ in range(columns)] for _ in range(rows)]
    pixels = resized.load()
    for y in range(resized.height):
        for x in range(resized.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha >= ALPHA_EMPTY_THRESHOLD:
                matrix[offset_y + y][offset_x + x] = nearest_colour(
                    palette, (red, green, blue), preset_size=preset_size
                ).code
    if background_code is not None:
        matrix = [
            [background_code if code is None else code for code in row]
            for row in matrix
        ]
    frozen_matrix = tuple(tuple(row) for row in matrix)
    counts = Counter(code for row in frozen_matrix for code in row if code is not None)
    empty_cells = sum(code is None for row in frozen_matrix for code in row)
    return Candidate(
        palette_id=palette.palette_id,
        palette_digest=palette.digest,
        preset_size=preset_size,
        columns=columns,
        rows=rows,
        board=board,
        background_code=background_code,
        source_sha256=_source_sha256(Path(source)),
        source_size=source_size,
        matrix=frozen_matrix,
        counts=dict(sorted(counts.items())),
        empty_cells=empty_cells,
    )
