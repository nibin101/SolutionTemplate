"""The computer-vision quality gate (app/quality.py).

Two independent checks, tested independently: blur (Laplacian variance) and
face presence (Haar cascade), the latter enforced only for views expected to
show a face. A note on scope - these tests do not attempt to prove a real
face is *detected*: Haar cascades are trained on photographs of real faces,
and a synthetic test image is not a reliable stand-in for one. What is worth
pinning, and what is pinned here, is the half that is deterministic: no face
present is always rejected when one is required, and never rejected when it
is not.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.quality import QualityConfig, assess_image, classify_view

DEFAULT_CFG = QualityConfig()


def _sharp_jpeg(width: int = 128, height: int = 96) -> bytes:
    """A checkerboard - real edges, so it is not blurry by construction."""
    image = Image.new("RGB", (width, height), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    step = 8
    for y in range(0, height, step):
        for x in range(0, width, step):
            if (x // step + y // step) % 2 == 0:
                draw.rectangle((x, y, x + step - 1, y + step - 1), fill=(230, 230, 230))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _blurry_jpeg(width: int = 128, height: int = 96) -> bytes:
    """A flat fill - zero high-frequency content, the definition of blurry."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (140, 140, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


# -- classify_view -----------------------------------------------------------


class TestClassifyView:
    def test_filename_with_a_required_keyword(self):
        assert classify_view("IMG_frontal_01.jpg", DEFAULT_CFG) == "face_required"

    def test_filename_with_an_optional_keyword(self):
        assert classify_view("profile_shot.jpg", DEFAULT_CFG) == "face_optional"

    def test_ordinary_camera_filename_needs_no_face(self):
        """A real DSLR never names a file this way - the common case."""
        assert classify_view("IMG_20260918_142233.JPG", DEFAULT_CFG) == "no_face"

    def test_intraoral_keyword_is_not_mistaken_for_a_face_shot(self):
        assert classify_view("upper_occlusal.jpg", DEFAULT_CFG) == "no_face"

    def test_view_hint_is_checked_even_when_the_filename_gives_no_clue(self):
        """The bridge simulator's own label, forwarded end to end, must work
        even for a filename a real camera would actually produce."""
        result = classify_view("SIM_0004.JPG", DEFAULT_CFG, view="Frontal smile")
        assert result == "face_required"

    def test_view_hint_does_not_override_a_non_matching_filename_falsely(self):
        result = classify_view("SIM_0002.JPG", DEFAULT_CFG, view="Right buccal")
        assert result == "no_face"

    def test_matching_is_case_insensitive(self):
        assert classify_view("FRONTAL.JPG", DEFAULT_CFG) == "face_required"


# -- assess_image: blur --------------------------------------------------


class TestBlurCheck:
    def test_sharp_photo_passes(self):
        result = assess_image(_sharp_jpeg(), "IMG_0001.JPG", DEFAULT_CFG)
        assert result.passed is True
        assert result.blur_rejected is False

    def test_blurry_photo_is_rejected(self):
        result = assess_image(_blurry_jpeg(), "IMG_0002.JPG", DEFAULT_CFG)
        assert result.passed is False
        assert result.blur_rejected is True
        assert "blurry" in result.reasons[0]

    def test_blur_check_can_be_turned_off(self):
        cfg = QualityConfig(blur_enabled=False)
        result = assess_image(_blurry_jpeg(), "IMG_0003.JPG", cfg)
        assert result.passed is True

    def test_threshold_is_configurable(self):
        """A practice that finds the default too strict can loosen it without
        a code change - this is the knob QUALITY_BLUR_MIN_VARIANCE controls."""
        lenient = QualityConfig(blur_min_variance=0.0)
        result = assess_image(_blurry_jpeg(), "IMG_0004.JPG", lenient)
        assert result.passed is True


# -- assess_image: face ----------------------------------------------------


class TestFaceCheck:
    def test_no_face_required_for_an_ordinary_camera_filename(self):
        """The common case: a real DSLR filename carries no view hint, so a
        sharp intraoral-looking photo must not be blocked on face presence."""
        result = assess_image(_sharp_jpeg(), "IMG_20260918_142233.JPG", DEFAULT_CFG)
        assert result.passed is True
        assert result.face_required is False

    def test_extraoral_filename_with_no_face_is_rejected(self):
        result = assess_image(_sharp_jpeg(), "frontal_repose.jpg", DEFAULT_CFG)
        assert result.passed is False
        assert result.face_rejected is True
        assert result.face_required is True
        assert "face" in result.reasons[0]

    def test_optional_keyword_never_rejects_on_a_missing_face(self):
        result = assess_image(_sharp_jpeg(), "profile.jpg", DEFAULT_CFG)
        assert result.passed is True
        assert result.face_required is False

    def test_view_hint_enforces_a_face_the_filename_would_not_have_caught(self):
        result = assess_image(_sharp_jpeg(), "SIM_0001.JPG", DEFAULT_CFG, view="Frontal smile")
        assert result.passed is False
        assert result.face_rejected is True

    def test_frontal_retracted_is_intraoral_not_a_face_shot(self):
        """The standard 5-view series' own "frontal retracted" is shot straight
        into retractor-held lips - no face in frame - unlike "frontal smile"
        or "frontal repose", which are genuine face portraits. This is a
        real-world naming collision, not just the demo's: found by actually
        running the bridge simulator, whose "Frontal retracted" label was
        being rejected for a face that view was never going to contain."""
        result = assess_image(_sharp_jpeg(), "SIM_0001.JPG", DEFAULT_CFG, view="Frontal retracted")
        assert result.passed is True
        assert result.face_required is False

    def test_intraoral_override_is_configurable(self):
        cfg = QualityConfig(intraoral_override_keywords=())
        result = assess_image(_sharp_jpeg(), "SIM_0001.JPG", cfg, view="Frontal retracted")
        assert result.passed is False, "with the override list empty, 'frontal' wins again"

    def test_face_check_can_be_turned_off(self):
        cfg = QualityConfig(face_enabled=False)
        result = assess_image(_sharp_jpeg(), "frontal.jpg", cfg)
        assert result.passed is True

    def test_keywords_are_configurable(self):
        """A practice with its own naming convention can point the gate at it."""
        cfg = QualityConfig(face_required_keywords=("headshot",))
        assert assess_image(_sharp_jpeg(), "frontal.jpg", cfg).passed is True
        assert assess_image(_sharp_jpeg(), "headshot.jpg", cfg).passed is False


# -- both checks together, and edge cases -----------------------------------


class TestGateBehaviour:
    def test_both_reasons_are_reported_together(self):
        """A blurry extraoral shot fails on two counts at once - both must be
        visible, not just whichever check happened to run first."""
        result = assess_image(_blurry_jpeg(), "frontal.jpg", DEFAULT_CFG)
        assert result.blur_rejected is True
        assert result.face_rejected is True
        assert len(result.reasons) == 2

    def test_gate_never_touches_the_source_bytes(self):
        original = _sharp_jpeg()
        before = bytes(original)
        assess_image(original, "IMG_0005.JPG", DEFAULT_CFG)
        assert original == before

    def test_an_undecodable_file_is_waved_through(self):
        """A RAW body file Pillow cannot open is not this gate's problem to
        solve - `chart_capture` already stores it without a preview, and the
        quality gate must not be the thing that blocks it instead."""
        result = assess_image(b"not-an-image-at-all", "IMG_0006.CR2", DEFAULT_CFG)
        assert result.passed is True

    def test_gate_disabled_entirely_passes_anything(self):
        cfg = QualityConfig(enabled=False)
        # enabled=False is read by the *caller* (chart_capture), not by
        # assess_image itself - confirm the config flag exists and defaults on.
        assert cfg.enabled is False
        assert DEFAULT_CFG.enabled is True


# -- through the real ingest path --------------------------------------------


def test_rejected_capture_never_reaches_the_chart(client, auth):
    """The point of running this before storage: a rejected image must leave
    no trace - not charted, not quarantined for review, not on disk."""
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("frontal.jpg", _blurry_jpeg(), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert "quality" in body

    assert client.get("/api/images/unassigned").json() == []
    assert client.get("/api/images/recent").json() == []


def test_sharp_capture_with_no_face_hint_is_charted_normally(client, auth):
    """The gate must not become a second, stricter quarantine path for the
    overwhelming majority of captures that need no face at all."""
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("IMG_20260918_090000.JPG", _sharp_jpeg(), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stored"


def test_view_hint_travels_through_the_ingest_form(client, auth):
    """End to end: the `view` field the bridge now forwards actually reaches
    the gate, for a filename that alone would not have required a face."""
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("SIM_0009.JPG", _sharp_jpeg(), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", "view": "Frontal smile"},
    )
    body = response.json()
    assert body["status"] == "rejected"
    assert body["quality"]["faces_detected"] == 0


def test_the_simulators_own_frontal_retracted_view_is_not_rejected(client, auth):
    """Regression check for the exact failure this was found by: running the
    real bridge simulator against a live backend rejected every "Frontal
    retracted" shot, forever, because that view is intraoral and can never
    contain a face."""
    response = client.post(
        "/api/ingest",
        headers=auth,
        files={"file": ("SIM_0010.JPG", _sharp_jpeg(), "image/jpeg")},
        data={"source": "test", "operatory": "OP-1", "view": "Frontal retracted"},
    )
    assert response.json()["status"] == "stored"
