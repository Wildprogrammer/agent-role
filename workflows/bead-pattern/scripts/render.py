from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import tempfile
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from palette import PaletteRevision


MIN_CELL_PIXELS = 40
MAX_CANVAS_PIXELS = 30_000_000
MIN_CANVAS_WIDTH = 640
LEGEND_COLUMN_WIDTH = 168
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
REPARSE_POINT = 0x400


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT)


def _checked_hub_root(hub_root: Path) -> Path:
    root = Path(hub_root).resolve(strict=True)
    if not root.is_dir() or _is_reparse(root):
        raise ValueError("hub root must be a normal existing directory")
    return root


def _ensure_normal_ancestors(root: Path, path: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("output directory escapes hub root")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise ValueError("output directory cannot use a symlink or reparse point")


def _output_directory(hub_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("run-id must match [a-z0-9][a-z0-9-]{0,63}")
    root = _checked_hub_root(hub_root)
    directory = root / "workflows" / "bead-pattern" / "outputs" / run_id
    _ensure_normal_ancestors(root, directory.parent)
    return directory


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in rgb)


def _text_colour(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = (0.2126 * rgb[0]) + (0.7152 * rgb[1]) + (0.0722 * rgb[2])
    return (20, 20, 20) if luminance >= 150 else (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _validate_accepted(accepted: Any, palette: PaletteRevision) -> tuple[tuple[tuple[str | None, ...], ...], Counter[str]]:
    required = (
        "palette_id",
        "palette_digest",
        "preset_size",
        "columns",
        "rows",
        "matrix",
        "counts",
        "empty_cells",
    )
    if any(not hasattr(accepted, field) for field in required):
        raise ValueError("accepted pattern is incomplete")
    if accepted.palette_id != palette.palette_id or accepted.palette_digest != palette.digest:
        raise ValueError("accepted pattern does not match the loaded palette revision")
    if accepted.preset_size not in palette.presets:
        raise ValueError("accepted pattern uses an unsupported palette preset")
    if not isinstance(accepted.columns, int) or not isinstance(accepted.rows, int):
        raise ValueError("accepted pattern dimensions are invalid")
    matrix = tuple(tuple(row) for row in accepted.matrix)
    if len(matrix) != accepted.rows or any(len(row) != accepted.columns for row in matrix):
        raise ValueError("accepted pattern matrix dimensions do not match its metadata")

    allowed = set(palette.presets[accepted.preset_size])
    counts: Counter[str] = Counter()
    empty_cells = 0
    for row in matrix:
        for code in row:
            if code is None:
                empty_cells += 1
            elif not isinstance(code, str) or code not in allowed:
                raise ValueError("accepted pattern contains a code outside its fixed palette preset")
            else:
                counts[code] += 1
    if dict(sorted(counts.items())) != dict(sorted(accepted.counts.items())):
        raise ValueError("accepted pattern colour counts do not match its frozen matrix")
    if empty_cells != accepted.empty_cells:
        raise ValueError("accepted pattern empty-cell count does not match its frozen matrix")
    return matrix, counts


def _measure_canvas(columns: int, rows: int, used_colours: int) -> tuple[int, int, int, int, int, int]:
    label_font = _font(16)
    code_font = _font(16)
    code_box = code_font.getbbox("M32")
    cell_size = max(MIN_CELL_PIXELS, (code_box[2] - code_box[0]) + 12)
    side = max(56, label_font.getbbox(str(rows))[2] + 18)
    header_height = 116
    width = max(MIN_CANVAS_WIDTH, side + (columns * cell_size) + 24)
    legend_columns = max(1, (width - 40) // LEGEND_COLUMN_WIDTH)
    legend_rows = (used_colours + legend_columns - 1) // legend_columns
    footer_height = max(96, 48 + (legend_rows * 28))
    height = header_height + (rows * cell_size) + footer_height + 24
    if width * height > MAX_CANVAS_PIXELS:
        raise ValueError(
            "readability or canvas limit exceeded; reduce the grid dimensions or use a smaller board"
        )
    return cell_size, side, header_height, width, height, legend_columns


def _draw_header(
    draw: ImageDraw.ImageDraw,
    accepted: Any,
    matrix: tuple[tuple[str | None, ...], ...],
    palette: PaletteRevision,
    width: int,
) -> None:
    title_font = _font(26)
    detail_font = _font(16)
    draw.text((20, 16), "BEAD PATTERN", fill=(25, 25, 25), font=title_font)
    board = accepted.board if accepted.board is not None else "custom"
    background = accepted.background_code if accepted.background_code is not None else "empty"
    details = (
        f"Grid {accepted.columns} x {accepted.rows}    Palette {accepted.preset_size}    "
        f"Board {board}    Background {background}"
    )
    draw.text((20, 55), details, fill=(45, 45, 45), font=detail_font)
    draw.line((20, 91, width - 20, 91), fill=(190, 190, 190), width=1)
    preview_limit = min(70, max(24, (width - 40) // 6))
    preview_scale = min(preview_limit / accepted.columns, preview_limit / accepted.rows)
    preview_width = max(1, round(accepted.columns * preview_scale))
    preview_height = max(1, round(accepted.rows * preview_scale))
    preview_x = width - preview_width - 20
    preview_y = 12
    for row, cells in enumerate(matrix):
        for column, code in enumerate(cells):
            colour = (255, 255, 255) if code is None else palette.colours[code].rgb
            draw.rectangle(
                (
                    preview_x + round(column * preview_width / accepted.columns),
                    preview_y + round(row * preview_height / accepted.rows),
                    preview_x + round((column + 1) * preview_width / accepted.columns),
                    preview_y + round((row + 1) * preview_height / accepted.rows),
                ),
                fill=colour,
            )
    draw.rectangle(
        (preview_x, preview_y, preview_x + preview_width, preview_y + preview_height),
        outline=(110, 110, 110),
        width=1,
    )


def _draw_grid(
    draw: ImageDraw.ImageDraw,
    matrix: tuple[tuple[str | None, ...], ...],
    palette: PaletteRevision,
    *,
    cell_size: int,
    side: int,
    top: int,
) -> None:
    label_font = _font(16)
    code_font = _font(16)
    for column in range(len(matrix[0])):
        text = str(column + 1)
        box = draw.textbbox((0, 0), text, font=label_font)
        x = side + (column * cell_size) + ((cell_size - (box[2] - box[0])) // 2)
        draw.text((x, top - 22), text, fill=(55, 55, 55), font=label_font)
    for row, cells in enumerate(matrix):
        y = top + (row * cell_size)
        text = str(row + 1)
        box = draw.textbbox((0, 0), text, font=label_font)
        draw.text((side - (box[2] - box[0]) - 9, y + ((cell_size - (box[3] - box[1])) // 2)), text, fill=(55, 55, 55), font=label_font)
        for column, code in enumerate(cells):
            x = side + (column * cell_size)
            fill = (255, 255, 255) if code is None else palette.colours[code].rgb
            draw.rectangle((x, y, x + cell_size, y + cell_size), fill=fill, outline=(145, 145, 145), width=1)
            if code is not None:
                box = draw.textbbox((0, 0), code, font=code_font)
                draw.text(
                    (x + ((cell_size - (box[2] - box[0])) // 2), y + ((cell_size - (box[3] - box[1])) // 2)),
                    code,
                    fill=_text_colour(palette.colours[code].rgb),
                    font=code_font,
                )


def _draw_legend(
    draw: ImageDraw.ImageDraw,
    palette: PaletteRevision,
    counts: Counter[str],
    *,
    side: int,
    top: int,
    legend_columns: int,
) -> None:
    heading_font = _font(18)
    text_font = _font(16)
    total = sum(counts.values())
    draw.text((20, top + 18), f"LEGEND  |  Total beads: {total}", fill=(25, 25, 25), font=heading_font)
    for index, code in enumerate(sorted(counts, key=lambda value: palette.colours[value].ordinal)):
        column = index % legend_columns
        row = index // legend_columns
        x = 20 + (column * LEGEND_COLUMN_WIDTH)
        y = top + 48 + (row * 28)
        colour = palette.colours[code]
        draw.rectangle((x, y, x + 22, y + 22), fill=colour.rgb, outline=(100, 100, 100), width=1)
        draw.text(
            (x + 32, y + 2),
            f"{code}  {_hex(colour.rgb)}  x {counts[code]}",
            fill=(30, 30, 30),
            font=text_font,
        )


def render_final_png(
    accepted: Any, *, palette: PaletteRevision, hub_root: Path, run_id: str
) -> Path:
    """Render only the accepted, frozen matrix to one readable PNG."""
    matrix, counts = _validate_accepted(accepted, palette)
    cell_size, side, header_height, width, height, legend_columns = _measure_canvas(
        accepted.columns, accepted.rows, len(counts)
    )
    directory = _output_directory(hub_root, run_id)
    if directory.exists():
        raise FileExistsError("refusing to overwrite an existing rendered run")
    directory.mkdir(parents=True, exist_ok=False)
    output = directory / "pattern.png"
    try:
        image = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        _draw_header(draw, accepted, matrix, palette, width)
        _draw_grid(
            draw,
            matrix,
            palette,
            cell_size=cell_size,
            side=side,
            top=header_height,
        )
        _draw_legend(
            draw,
            palette,
            counts,
            side=side,
            top=header_height + (accepted.rows * cell_size),
            legend_columns=legend_columns,
        )
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".png", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            image.save(temporary_path, format="PNG", optimize=True)
            temporary_path.replace(output)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return output
    except BaseException:
        if output.exists():
            output.unlink()
        directory.rmdir()
        raise
