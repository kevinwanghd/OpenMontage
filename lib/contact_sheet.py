"""Server-side contact sheets for keyframe review (VIS-03 / D-10).

A contact sheet is an L1 review artifact: one Pillow-composited PNG per page
with every candidate image placed in a locatable cell. The accompanying JSON
index is the bridge to L2: each cell records the sheet page, the exact pixel
rectangle that should open the original media, and the original file path. The
review surface never needs to know the layout algorithm; it reads the index
(``l1`` to render the sheet, ``l2`` to fetch the full-resolution candidate).

Paging rules:
- Candidate order follows shot order, then candidate order.
- A page holds at most ``columns * rows`` cells.
- Page breaks happen between shots whenever the next shot does not fit whole,
  so a shot's candidates are normally reviewed together. A shot with more
  candidates than one page can hold continues on the next page.

L2 is the original media itself; this module never downscales or copies it.
If Pillow is unavailable the L1 sheet cannot be built and the caller is told
to fall back to L2 (no heavy new dependency is added here).

M0: local files only, zero external API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

try:  # Pillow is already a repo dependency; absence degrades to L2 only.
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False


INDEX_FILENAME = "contact_sheet_index.json"
INDEX_VERSION = "1.0"

_DEFAULT_BACKGROUND = (20, 20, 24)
_CELL_BACKGROUND = (40, 40, 46)
_BORDER = (96, 96, 108)
_LABEL = (232, 232, 238)
_HEADER = (170, 170, 182)

Value = int | float


class ContactSheetError(Exception):
    """The contact sheet request is invalid or cannot be satisfied."""


class PillowUnavailableError(ContactSheetError):
    """Pillow is missing, so L1 sheets cannot be produced; use L2 originals."""


def _require_pillow() -> None:
    if not PILLOW_AVAILABLE or Image is None:
        raise PillowUnavailableError(
            "Pillow is not installed: L1 contact sheets are unavailable. "
            "Serve L2 original media for review, and install Pillow (already "
            "in requirements.txt) to re-enable server-side sheets."
        )


def _rect(x: Value, y: Value, width: Value, height: Value) -> dict[str, Value]:
    return {
        "x": round(float(x), 3),
        "y": round(float(y), 3),
        "width": round(float(width), 3),
        "height": round(float(height), 3),
    }


def _normalise_candidate(
    shot_id: str, raw: Any, index: int
) -> dict[str, Any]:
    if isinstance(raw, (str, Path)):
        candidate: dict[str, Any] = {"path": str(raw)}
    elif isinstance(raw, dict):
        candidate = dict(raw)
    else:
        raise ContactSheetError(
            f"shot {shot_id!r} candidate #{index + 1} must be a path or dict, "
            f"got {type(raw).__name__}"
        )

    path = candidate.get("path", candidate.get("original_path"))
    if not path:
        raise ContactSheetError(
            f"shot {shot_id!r} candidate #{index + 1} has no path"
        )
    candidate_id = candidate.get("candidate_id") or f"{shot_id}-c{index + 1}"
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ContactSheetError(
            f"shot {shot_id!r} candidate #{index + 1} has an empty candidate_id"
        )

    source = Path(str(path))
    if not source.is_file():
        raise FileNotFoundError(
            f"candidate {candidate_id!r} image not found: {source}"
        )

    metadata = candidate.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ContactSheetError(
            f"candidate {candidate_id!r} metadata must be a dict"
        )

    return {
        "candidate_id": candidate_id,
        "source_path": str(source),
        "metadata": metadata,
    }


def _normalise_shots(shots: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(shots, (list, tuple)):
        raise ContactSheetError(
            f"shots must be a list, got {type(shots).__name__}"
        )

    normalised: list[dict[str, Any]] = []
    seen_shots: set[str] = set()
    seen_candidates: set[str] = set()

    for shot_index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            raise ContactSheetError(
                f"shot #{shot_index + 1} must be a dict, got {type(shot).__name__}"
            )
        shot_id = shot.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise ContactSheetError(f"shot #{shot_index + 1} has no shot_id")
        if shot_id in seen_shots:
            raise ContactSheetError(f"duplicate shot_id {shot_id!r}")
        seen_shots.add(shot_id)

        raw_candidates = shot.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
            raise ContactSheetError(f"shot {shot_id!r} has no candidates")

        candidates = []
        for candidate_index, raw in enumerate(raw_candidates):
            candidate = _normalise_candidate(shot_id, raw, candidate_index)
            candidate_id = candidate["candidate_id"]
            if candidate_id in seen_candidates:
                raise ContactSheetError(f"duplicate candidate_id {candidate_id!r}")
            seen_candidates.add(candidate_id)
            candidates.append(candidate)

        normalised.append({"shot_id": shot_id, "candidates": candidates})

    if not normalised:
        raise ContactSheetError("no shots to compose into a contact sheet")
    return normalised


def _plan_pages(
    shots: list[dict[str, Any]], capacity: int
) -> list[dict[str, Any]]:
    """Pack candidates into pages, keeping each shot together when it fits."""
    pages: list[dict[str, Any]] = []
    current: dict[str, Any] = {"shot_ids": [], "candidates": []}

    def flush() -> None:
        nonlocal current
        if not current["candidates"]:
            return
        current["page_index"] = len(pages)
        pages.append(current)
        current = {"shot_ids": [], "candidates": []}

    for shot in shots:
        candidates = shot["candidates"]
        remaining = capacity - len(current["candidates"])
        if current["candidates"] and len(candidates) > remaining:
            flush()
        if shot["shot_id"] not in current["shot_ids"]:
            current["shot_ids"].append(shot["shot_id"])
        offset = 0
        while offset < len(candidates):
            space = capacity - len(current["candidates"])
            if space <= 0:
                flush()
                if shot["shot_id"] not in current["shot_ids"]:
                    current["shot_ids"].append(shot["shot_id"])
                space = capacity
            take = min(space, len(candidates) - offset)
            current["candidates"].extend(candidates[offset : offset + take])
            offset += take
            if offset < len(candidates):
                # A single shot spans pages; keep the shot id on each page.
                flush()
                if shot["shot_id"] not in current["shot_ids"]:
                    current["shot_ids"].append(shot["shot_id"])
    flush()
    return pages


def _paste_candidate(
    page: Any,
    draw: Any,
    candidate: dict[str, Any],
    slot_x: int,
    slot_y: int,
    cell_width: int,
    cell_height: int,
    label_height: int,
    draw_label: bool,
    font: Any,
) -> dict[str, Any]:
    image_area_height = cell_height - label_height
    with Image.open(candidate["source_path"]) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        thumb = ImageOps.contain(image, (cell_width, image_area_height))

    thumb_width, thumb_height = thumb.size
    image_x = slot_x + (cell_width - thumb_width) // 2
    image_y = slot_y + label_height + (image_area_height - thumb_height) // 2
    page.paste(thumb, (image_x, image_y))

    draw.rectangle(
        [image_x, image_y, image_x + thumb_width - 1, image_y + thumb_height - 1],
        outline=_BORDER,
        width=2,
    )
    if draw_label and font is not None:
        draw.text(
            (slot_x + 4, slot_y + 3),
            candidate["candidate_id"],
            fill=_LABEL,
            font=font,
        )

    return {
        "candidate_id": candidate["candidate_id"],
        "image_rect": _rect(image_x, image_y, thumb_width, thumb_height),
    }


def build_contact_sheets(
    shots: Sequence[dict[str, Any]],
    output_dir: str | Path,
    *,
    columns: int = 2,
    rows: int = 2,
    cell_width: int = 270,
    cell_height: int = 480,
    margin: int = 16,
    gap: int = 12,
    label: bool = True,
    filename_prefix: str = "contact_sheet",
    background: tuple[int, int, int] = _DEFAULT_BACKGROUND,
    write_index: bool = True,
) -> dict[str, Any]:
    """Compose candidate images into paginated contact sheets plus an index.

    Args:
        shots: Ordered ``[{"shot_id": ..., "candidates": [...]}]``. A candidate
            is either a path string or ``{"candidate_id": ..., "path": ...}``.
        output_dir: Directory that receives the sheets and the JSON index.
        columns/rows: Grid size; ``columns * rows`` is the page capacity.
        cell_width/cell_height: Cell slot size before the label strip.
        margin/gap: Page margin and space between cells.
        label: Draw the candidate id in each cell.
        filename_prefix: Sheet filename prefix (no path separators).
        background: Page background RGB.
        write_index: Write ``contact_sheet_index.json`` beside the sheets.

    Returns:
        The index dict (also written to disk when ``write_index`` is true).

    Raises:
        PillowUnavailableError: Pillow is missing; L1 is degraded to L2 only.
        ContactSheetError: malformed request or impossible layout.
        FileNotFoundError: a candidate image does not exist.
    """
    _require_pillow()

    for name, value in (
        ("columns", columns),
        ("rows", rows),
        ("cell_width", cell_width),
        ("cell_height", cell_height),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ContactSheetError(f"{name} must be a positive integer")
    if margin < 0 or gap < 0:
        raise ContactSheetError("margin and gap must be non-negative")
    if not isinstance(filename_prefix, str) or not filename_prefix:
        raise ContactSheetError("filename_prefix must be a non-empty string")
    if Path(filename_prefix).name != filename_prefix:
        raise ContactSheetError("filename_prefix must not contain path separators")

    normalised = _normalise_shots(shots)
    capacity = columns * rows
    pages = _plan_pages(normalised, capacity)

    label_height = 18 if label else 0
    page_width = margin * 2 + columns * cell_width + (columns - 1) * gap
    header_height = 26
    page_height = (
        header_height + margin * 2 + rows * cell_height + (rows - 1) * gap
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    font = ImageFont.load_default() if label else None
    page_records: list[dict[str, Any]] = []
    shots_index: list[dict[str, Any]] = [
        {"shot_id": shot["shot_id"], "candidates": []} for shot in normalised
    ]
    shots_by_id = {record["shot_id"]: record for record in shots_index}
    entries_by_candidate: dict[str, dict[str, Any]] = {}

    for page in pages:
        page_index = page["page_index"]
        page_name = f"{filename_prefix}_{page_index + 1:04d}.png"
        page_file = output_path / page_name
        sheet = Image.new("RGB", (page_width, page_height), background)
        draw = ImageDraw.Draw(sheet)

        if font is not None:
            draw.text(
                (margin, 6),
                f"Page {page_index + 1}/{len(pages)} - contact sheet",
                fill=_HEADER,
                font=font,
            )

        cells: list[dict[str, Any]] = []
        for cell_index, candidate in enumerate(page["candidates"]):
            column = cell_index % columns
            row = cell_index // columns
            slot_x = margin + column * (cell_width + gap)
            slot_y = header_height + margin + row * (cell_height + gap)
            draw.rectangle(
                [slot_x, slot_y, slot_x + cell_width - 1, slot_y + cell_height - 1],
                fill=_CELL_BACKGROUND,
            )
            placed = _paste_candidate(
                sheet,
                draw,
                candidate,
                slot_x,
                slot_y,
                cell_width,
                cell_height,
                label_height,
                label,
                font,
            )
            cell = {
                "shot_id": _shot_id_for_candidate(normalised, candidate["candidate_id"]),
                "candidate_id": candidate["candidate_id"],
                "cell": _rect(slot_x, slot_y, cell_width, cell_height),
                "image_rect": placed["image_rect"],
                "click_region": dict(placed["image_rect"]),
                "page_index": page_index,
                "page_path": page_name,
                "page_file": str(page_file),
            }
            entries_by_candidate[candidate["candidate_id"]] = cell
            cells.append(cell)

        sheet.save(page_file, format="PNG")
        page_records.append({
            "page_index": page_index,
            "page_number": page_index + 1,
            "path": page_name,
            "file": str(page_file),
            "width": page_width,
            "height": page_height,
            "shot_ids": list(page["shot_ids"]),
            "cells": cells,
        })

    for shot in normalised:
        record = shots_by_id[shot["shot_id"]]
        for candidate in shot["candidates"]:
            cell = entries_by_candidate[candidate["candidate_id"]]
            record["candidates"].append({
                "candidate_id": candidate["candidate_id"],
                "shot_id": shot["shot_id"],
                "page_index": cell["page_index"],
                "page_path": cell["page_path"],
                "page_file": cell["page_file"],
                "cell": cell["cell"],
                "image_rect": cell["image_rect"],
                "click_region": cell["click_region"],
                "l1": {
                    "kind": "contact_sheet",
                    "page_index": cell["page_index"],
                    "page_path": cell["page_path"],
                    "click_region": cell["click_region"],
                },
                "l2": {
                    "kind": "original_media",
                    "path": candidate["source_path"],
                },
                "source_path": candidate["source_path"],
                "metadata": candidate["metadata"],
            })

    index = {
        "version": INDEX_VERSION,
        "kind": "contact_sheet_index",
        "layout": {
            "columns": columns,
            "rows": rows,
            "capacity": capacity,
            "cell_width": cell_width,
            "cell_height": cell_height,
            "margin": margin,
            "gap": gap,
        },
        "page_count": len(page_records),
        "pages": page_records,
        "shots": shots_index,
    }

    if write_index:
        (output_path / INDEX_FILENAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return index


def _shot_id_for_candidate(
    shots: list[dict[str, Any]], candidate_id: str
) -> str:
    for shot in shots:
        for candidate in shot["candidates"]:
            if candidate["candidate_id"] == candidate_id:
                return shot["shot_id"]
    raise ContactSheetError(f"candidate {candidate_id!r} has no shot")