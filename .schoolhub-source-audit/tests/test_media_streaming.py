import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from workspace import WorkspaceManager, WorkspaceError

PASSWORD = "SchoolHub-Media-Test-42!"


class MediaStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = base / "Workspace"
        self.vault = base / "Vault.vault"
        self.ws.mkdir()
        self.wm = WorkspaceManager(self.ws, self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_media_set(self):
        samples = {
            "image.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40,
            "photo.jpg": b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 2048 + b"\xff\xd9",
            "sound.mp3": b"ID3\x04\x00\x00" + b"MP3DATA" * 4096,
            "audio.wav": b"RIFF" + b"\x00" * 36 + b"WAVE" + b"AUDIODATA" * 4096,
            "video.mp4": b"\x00\x00\x00\x18ftypmp42" + b"VIDEODATA" * 8192,
        }
        for name, data in samples.items():
            (self.ws / name).write_bytes(data)
        return {k: bytes(v) for k, v in samples.items()}

    def test_binary_media_roundtrip(self):
        expected = self._make_media_set()
        self.wm.create(PASSWORD)
        for name in expected:
            enc = self.vault / "files" / name
            self.assertEqual(enc.read_bytes()[:6], b"SHENC2")
        self.wm.unlock(PASSWORD)
        for name, data in expected.items():
            self.assertEqual((self.ws / name).read_bytes(), data)

    def test_large_video_streaming_roundtrip(self):
        path = self.ws / "large-video.mp4"
        block = bytes(range(256)) * 4096  # 1 MiB
        with path.open("wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42")
            for _ in range(24):
                fh.write(block)
        expected_size = path.stat().st_size
        self.wm.create(PASSWORD)
        self.wm.unlock(PASSWORD)
        self.assertEqual(path.stat().st_size, expected_size)
        with path.open("rb") as fh:
            self.assertEqual(fh.read(12), b"\x00\x00\x00\x18ftypmp42")

    def test_legacy_shenc1_is_still_readable(self):
        (self.ws / "legacy.bin").write_bytes(b"legacy-binary-data\x00\xff" * 100)
        self.wm.create(PASSWORD)
        key = self.wm._verify_password(PASSWORD)
        rel = Path("legacy.bin")
        plain = b"legacy-binary-data\x00\xff" * 100
        nonce = os.urandom(self.wm.NONCE_LEN)
        aad = self.wm.FILE_AAD_PREFIX + b"legacy.bin"
        payload = self.wm.FILE_MAGIC_V1 + nonce + AESGCM(key).encrypt(nonce, plain, aad)
        (self.vault / "files" / rel).write_bytes(payload)
        self.wm.unlock(PASSWORD)
        self.assertEqual((self.ws / rel).read_bytes(), plain)

    def test_truncated_stream_is_detected(self):
        (self.ws / "clip.mp4").write_bytes(b"X" * (5 * 1024 * 1024))
        self.wm.create(PASSWORD)
        enc = self.vault / "files" / "clip.mp4"
        data = enc.read_bytes()
        enc.write_bytes(data[:-31])
        with self.assertRaises(WorkspaceError):
            self.wm.unlock(PASSWORD)


if __name__ == "__main__":
    unittest.main()
