import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_game_player.storage.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMetadata,
    ArtifactStore,
    FileSystemArtifactStore,
)


class ArtifactStoreTest(unittest.TestCase):
    def test_metadata_schema_is_valid_json(self):
        path = Path(__file__).parents[1] / "config" / "artifact_metadata.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("object", schema["type"])
        self.assertIn("sha256", schema["required"])

    def test_stores_content_by_sha256_and_round_trips_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            payload = b"frame bytes\x00\x01"
            reference = store.put(payload, media_type="image/png", artifact_type="frame.raw", sensitive=True)

            self.assertEqual(hashlib.sha256(payload).hexdigest(), reference.sha256)
            self.assertEqual(f"sha256.{reference.sha256}", reference.artifact_id)
            self.assertEqual(payload, store.read(reference))
            metadata = store.verify(reference)
            restored = ArtifactMetadata.from_dict(json.loads(json.dumps(metadata.to_dict())))
            self.assertEqual(metadata, restored)
            self.assertTrue(metadata.sensitive)
            self.assertEqual("frame.raw", metadata.artifact_type)
            self.assertEqual(len(payload), metadata.size_bytes)
            self.assertIsInstance(store, ArtifactStore)

    def test_duplicate_content_reuses_object_and_conflicting_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            first = store.put(b"same bytes", media_type="application/octet-stream", sensitive=False)
            second = store.put(b"same bytes", media_type="application/octet-stream", sensitive=False)

            self.assertEqual(first, second)
            self.assertEqual(1, len(tuple(store._iter_digests())))
            with self.assertRaisesRegex(ArtifactIntegrityError, "metadata does not match"):
                store.put(b"same bytes", media_type="image/png", sensitive=False)
            with self.assertRaisesRegex(ArtifactIntegrityError, "artifact_type"):
                store.put(b"same bytes", media_type="application/octet-stream", artifact_type="video.segment")

    def test_reference_integrity_detects_missing_tampered_and_wrong_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileSystemArtifactStore(root)
            reference = store.put(b"original", media_type="application/octet-stream")
            payload_path = root / reference.sha256[:2] / reference.sha256 / "payload"
            payload_path.write_bytes(b"tampered")

            issues = store.check_integrity((reference,))

            self.assertEqual(1, len(issues))
            self.assertIn("content hash mismatch", issues[0].reason)
            missing = reference.__class__(
                f"sha256.{'0' * 64}", "0" * 64, "application/octet-stream", False
            )
            self.assertIn("not found", store.check_integrity((missing,))[0].reason)

    def test_store_wide_integrity_check_detects_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileSystemArtifactStore(root)
            reference = store.put(b"payload", media_type="application/octet-stream")
            metadata_path = root / reference.sha256[:2] / reference.sha256 / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["size_bytes"] += 1
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            issues = store.check_integrity()

            self.assertEqual(1, len(issues))
            self.assertIn("size does not match", issues[0].reason)

    def test_finds_unreferenced_objects_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            kept = store.put(b"referenced", media_type="application/octet-stream")
            orphan = store.put(b"not referenced", media_type="video/mp4", sensitive=True)

            self.assertEqual((orphan.sha256,), store.find_orphans((kept,)))
            self.assertEqual(b"not referenced", store.read(orphan))

    def test_failed_staging_write_never_publishes_partial_object(self):
        class BrokenMetadataWriteStore(FileSystemArtifactStore):
            @staticmethod
            def _write_staged_file(path: Path, content: bytes) -> None:
                if path.name == "metadata.json":
                    raise OSError("simulated metadata write failure")
                FileSystemArtifactStore._write_staged_file(path, content)

        with tempfile.TemporaryDirectory() as directory:
            store = BrokenMetadataWriteStore(Path(directory))
            with self.assertRaisesRegex(OSError, "simulated"):
                store.put(b"payload", media_type="application/octet-stream")

            self.assertEqual((), tuple(store._iter_digests()))
            self.assertEqual([], list(Path(directory).glob("**/.pending-*")))

    def test_concurrent_duplicate_writes_publish_one_valid_object(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            payload = b"same concurrent payload" * 1024
            with ThreadPoolExecutor(max_workers=6) as pool:
                references = tuple(
                    pool.map(
                        lambda _: store.put(payload, media_type="application/octet-stream"),
                        range(12),
                    )
                )

            self.assertTrue(all(reference == references[0] for reference in references))
            self.assertEqual(1, len(tuple(store._iter_digests())))
            self.assertEqual(payload, store.read(references[0]))


if __name__ == "__main__":
    unittest.main()
