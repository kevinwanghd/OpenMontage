"""MiniMax subject references must be inlined, not sent as local paths.

Measured 2026-11-17: passing a filesystem path to `subject_reference.image_file`
fails with "disallowed image url: localhost or private address not allowed",
because the API resolves the value server-side. A data URI works and is what the
tool now builds. Without this test the behaviour is one refactor away from
returning to a silent 400.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tools.graphics.minimax_image import MiniMaxImage


@pytest.fixture()
def image(tmp_path) -> Path:
    pil = pytest.importorskip("PIL.Image")
    path = tmp_path / "card.png"
    pil.new("RGBA", (64, 96), (10, 20, 30, 255)).save(path)
    return path


class TestInlineReference:
    def test_local_path_becomes_data_uri(self, image):
        value = MiniMaxImage._inline_reference_file(str(image))
        assert value.startswith("data:image/jpeg;base64,")
        payload = base64.b64decode(value.split(",", 1)[1])
        assert payload[:2] == b"\xff\xd8"  # JPEG magic

    def test_http_url_is_left_alone(self):
        url = "https://example.com/card.png"
        assert MiniMaxImage._inline_reference_file(url) == url

    def test_existing_data_uri_is_left_alone(self):
        uri = "data:image/png;base64,AAAA"
        assert MiniMaxImage._inline_reference_file(uri) == uri

    def test_unknown_path_is_returned_unchanged(self):
        """A non-file value is the caller's problem; do not silently invent one."""
        assert MiniMaxImage._inline_reference_file("/nope/missing.png") == "/nope/missing.png"

    def test_normalise_walks_the_reference_list(self, image):
        references = [{"type": "character", "image_file": str(image)}]
        normalised = MiniMaxImage._normalise_subject_references(references)
        assert normalised[0]["image_file"].startswith("data:image/jpeg;base64,")
        # The caller's list must not be mutated.
        assert references[0]["image_file"] == str(image)

    def test_payload_uses_inlined_reference(self, image):
        payload = MiniMaxImage._build_payload({
            "prompt": "a woman",
            "model": "image-01",
            "subject_reference": [{"type": "character", "image_file": str(image)}],
        })
        assert payload["subject_reference"][0]["image_file"].startswith("data:image/jpeg")