from __future__ import annotations

import types
import unittest

from app.bot import pick_upload_payload, render_upload_progress_text


def _message(**kwargs: object) -> object:
    fields = {
        "document": None,
        "video": None,
        "audio": None,
        "animation": None,
        "voice": None,
        "video_note": None,
        "sticker": None,
        "photo": None,
    }
    fields.update(kwargs)
    return types.SimpleNamespace(**fields)


class UploadHelpersTests(unittest.TestCase):
    def test_pick_upload_payload_document(self) -> None:
        document = types.SimpleNamespace(file_id="f1", file_name="archive.tar", file_size=1234)
        payload = pick_upload_payload(_message(document=document))
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.file_name, "archive.tar")
        self.assertEqual(payload.file_size, 1234)

    def test_pick_upload_payload_photo_uses_generated_name(self) -> None:
        photo = [types.SimpleNamespace(file_id="p1", file_unique_id="u1", file_size=500)]
        payload = pick_upload_payload(_message(photo=photo))
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.file_name, "photo_u1.jpg")
        self.assertEqual(payload.file_id, "p1")

    def test_pick_upload_payload_sticker_extension(self) -> None:
        sticker = types.SimpleNamespace(
            file_id="s1",
            file_unique_id="suni",
            file_size=42,
            is_video=True,
            is_animated=False,
        )
        payload = pick_upload_payload(_message(sticker=sticker))
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.file_name, "sticker_suni.webm")

    def test_render_upload_progress_text_with_percent(self) -> None:
        text = render_upload_progress_text("video.mp4", 512, 1024)
        self.assertIn("50%", text)
        self.assertIn("video.mp4", text)


if __name__ == "__main__":
    unittest.main()
