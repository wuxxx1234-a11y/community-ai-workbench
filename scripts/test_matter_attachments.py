#!/usr/bin/env python3
"""MATTER附件V1 T1-T24：仅TEMP与随机字节fixture。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import matter_attachments as tool


CONFIRMED_AT = "2026-08-24T10:00:00+08:00"


class MatterAttachmentV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="matter-attachment-v1-")
        self.base = Path(self.temp.name)
        self.project_root = self.base / "project"
        self.ledger = self.project_root / "01_事项台账"
        self.ledger.mkdir(parents=True)
        self.approved_root = self.base / "approved-trae-attachments"
        self.approved_root.mkdir()
        self.attachment_root = self.base / "formal-attachments"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def matter(self, matter_id: str, suffix: str = "fixture") -> Path:
        path = self.ledger / f"{matter_id}_{suffix}.md"
        path.write_text(f"# {matter_id}｜虚构事项\n", encoding="utf-8")
        return path

    def source(self, name: str = "fixture.png", size: int = 257) -> Path:
        path = self.approved_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
        return path

    def capture(
        self,
        matter_id: str,
        source: Path,
        mode: str = tool.ATTRIBUTION_MECHANICAL,
        **kwargs: object,
    ) -> dict[str, object]:
        return tool.capture_attachment(
            matter_id,
            source,
            mode,
            project_root=self.project_root,
            attachment_root=self.attachment_root,
            approved_source_root=self.approved_root,
            **kwargs,
        )

    def assert_error(self, code: str, callback: object) -> tool.AttachmentError:
        with self.assertRaises(tool.AttachmentError) as caught:
            callback()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def manifest(self, matter_id: str) -> dict[str, object]:
        path = self.attachment_root / matter_id / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_t01_mechanical_capture_complete(self) -> None:
        self.matter("MATTER-101")
        source = self.source()
        result = self.capture("MATTER-101", source)
        self.assertEqual(result["status"], "FORMAL-ATTACHMENT-CAPTURE-COMPLETE")
        self.assertEqual(result["byte_size"], source.stat().st_size)
        status = tool.status_matter(
            "MATTER-101",
            project_root=self.project_root,
            attachment_root=self.attachment_root,
        )
        self.assertEqual(status["status"], "CONSISTENT")

    def test_t02_model_confirmed_capture_complete(self) -> None:
        self.matter("MATTER-102")
        source = self.source("fixture.jpeg")
        result = self.capture(
            "MATTER-102",
            source,
            tool.ATTRIBUTION_MODEL_CONFIRMED,
            attribution_confirmed_at=CONFIRMED_AT,
            source_media_type="image/jpeg",
            source_turn_id="fictional-turn",
        )
        self.assertEqual(result["status"], "FORMAL-ATTACHMENT-CAPTURE-COMPLETE")
        record = self.manifest("MATTER-102")["attachments"][0]  # type: ignore[index]
        self.assertEqual(record["attribution_mode"], tool.ATTRIBUTION_MODEL_CONFIRMED)

    def test_t03_same_matter_same_sha_is_idempotent(self) -> None:
        self.matter("MATTER-103")
        source = self.source("first.jpeg")
        first = self.capture("MATTER-103", source)
        before = self.manifest("MATTER-103")["attachments"][0]["captured_at"]  # type: ignore[index]
        same_bytes_different_suffix = self.approved_root / "second.png"
        same_bytes_different_suffix.write_bytes(source.read_bytes())
        second = self.capture("MATTER-103", same_bytes_different_suffix)
        after = self.manifest("MATTER-103")["attachments"][0]["captured_at"]  # type: ignore[index]
        files = list((self.attachment_root / "MATTER-103").glob("MATTER-103__*"))
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["status"], "ALREADY-CAPTURED")
        self.assertEqual(before, after)
        self.assertEqual(len(files), 1)

    def test_t04_same_sha_different_matters_are_separate(self) -> None:
        self.matter("MATTER-104")
        self.matter("MATTER-105")
        source = self.source()
        one = self.capture("MATTER-104", source)
        two = self.capture("MATTER-105", source)
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertNotEqual(one["stored_relative_path"], two["stored_relative_path"])
        self.assertEqual(len(self.manifest("MATTER-104")["attachments"]), 1)  # type: ignore[arg-type]
        self.assertEqual(len(self.manifest("MATTER-105")["attachments"]), 1)  # type: ignore[arg-type]
        self.assertNotIn("relationships", self.manifest("MATTER-104"))

    def test_t05_invalid_matter_id_writes_nothing(self) -> None:
        source = self.source()
        self.assert_error(
            "INVALID-MATTER-ID",
            lambda: self.capture("BAD-005", source),
        )
        self.assertFalse(self.attachment_root.exists())

    def test_t06_missing_matter_writes_nothing(self) -> None:
        source = self.source()
        self.assert_error(
            "MATTER-NOT-FOUND",
            lambda: self.capture("MATTER-106", source),
        )
        self.assertFalse(self.attachment_root.exists())

    def test_t07_duplicate_matter_files_writes_nothing(self) -> None:
        self.matter("MATTER-107", "one")
        self.matter("MATTER-107", "two")
        source = self.source()
        self.assert_error(
            "MATTER-NOT-UNIQUE",
            lambda: self.capture("MATTER-107", source),
        )
        self.assertFalse(self.attachment_root.exists())

    def test_t08_source_outside_approved_root_rejected(self) -> None:
        self.matter("MATTER-108")
        outside = self.base / "outside.png"
        outside.write_bytes(os.urandom(50))
        self.assert_error(
            "SOURCE-OUTSIDE-APPROVED-ROOT",
            lambda: self.capture("MATTER-108", outside),
        )

    def test_t09_traversal_and_resolved_escape_rejected(self) -> None:
        self.matter("MATTER-109")
        outside = self.base / "outside.png"
        outside.write_bytes(os.urandom(50))
        traversal = self.approved_root / ".." / "outside.png"
        self.assert_error(
            "SOURCE-OUTSIDE-APPROVED-ROOT",
            lambda: self.capture("MATTER-109", traversal),
        )
        link = self.approved_root / "escape.png"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        self.assert_error(
            "SOURCE-OUTSIDE-APPROVED-ROOT",
            lambda: self.capture("MATTER-109", link),
        )

    def test_t10_source_directory_rejected(self) -> None:
        self.matter("MATTER-110")
        directory = self.approved_root / "directory.png"
        directory.mkdir()
        self.assert_error(
            "SOURCE-NOT-REGULAR-FILE",
            lambda: self.capture("MATTER-110", directory),
        )

    def test_t11_missing_source_rejected(self) -> None:
        self.matter("MATTER-111")
        missing = self.approved_root / "missing.png"
        self.assert_error(
            "SOURCE-NOT-FOUND",
            lambda: self.capture("MATTER-111", missing),
        )

    def test_t12_unsupported_attachment_type_rejected(self) -> None:
        self.matter("MATTER-112")
        source = self.source("fixture.txt")
        self.assert_error(
            "UNSUPPORTED-ATTACHMENT-TYPE",
            lambda: self.capture("MATTER-112", source),
        )

    def test_t13_corrupt_copy_never_commits_complete_manifest(self) -> None:
        self.matter("MATTER-113")
        source = self.source()

        def corrupt_copy(source_path: Path, target_path: Path) -> None:
            with source_path.open("rb") as src, target_path.open("xb") as dst:
                dst.write(src.read())
                dst.write(b"corruption")
                dst.flush()
                os.fsync(dst.fileno())

        with mock.patch.object(tool, "_copy_stream", side_effect=corrupt_copy):
            self.assert_error(
                "TARGET-SHA-MISMATCH",
                lambda: self.capture("MATTER-113", source),
            )
        manifest = self.attachment_root / "MATTER-113" / "manifest.json"
        self.assertFalse(manifest.exists())
        self.assertFalse(list((self.attachment_root / "MATTER-113").glob("MATTER-113__*")))

    def test_t14_corrupt_manifest_is_not_overwritten(self) -> None:
        self.matter("MATTER-114")
        source = self.source()
        matter_dir = self.attachment_root / "MATTER-114"
        matter_dir.mkdir(parents=True)
        manifest = matter_dir / "manifest.json"
        original = "{not-json\n"
        manifest.write_text(original, encoding="utf-8")
        self.assert_error(
            "CORRUPT-MANIFEST",
            lambda: self.capture("MATTER-114", source),
        )
        self.assertEqual(manifest.read_text(encoding="utf-8"), original)

    def test_t15_unsupported_manifest_version_is_not_overwritten(self) -> None:
        self.matter("MATTER-115")
        source = self.source()
        matter_dir = self.attachment_root / "MATTER-115"
        matter_dir.mkdir(parents=True)
        manifest = matter_dir / "manifest.json"
        original = {
            "manifest_version": "999",
            "matter_id": "MATTER-115",
            "attachments": [],
        }
        manifest.write_text(json.dumps(original), encoding="utf-8")
        self.assert_error(
            "UNSUPPORTED-MANIFEST-VERSION",
            lambda: self.capture("MATTER-115", source),
        )
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), original)

    def test_t16_manifest_record_with_missing_file_is_reported(self) -> None:
        self.matter("MATTER-116")
        source = self.source()
        result = self.capture("MATTER-116", source)
        target = self.attachment_root.joinpath(
            *Path(result["stored_relative_path"]).parts  # type: ignore[arg-type]
        )
        target.unlink()
        status = tool.status_matter(
            "MATTER-116",
            project_root=self.project_root,
            attachment_root=self.attachment_root,
        )
        self.assertEqual(status["status"], "INCONSISTENT")
        self.assertIn("MISSING-FORMAL-FILE", {x["code"] for x in status["anomalies"]})

    def test_t17_formal_file_without_manifest_is_orphan(self) -> None:
        self.matter("MATTER-117")
        payload = os.urandom(80)
        sha256 = hashlib.sha256(payload).hexdigest()
        matter_dir = self.attachment_root / "MATTER-117"
        matter_dir.mkdir(parents=True)
        (matter_dir / f"MATTER-117__{sha256}.png").write_bytes(payload)
        status = tool.status_matter(
            "MATTER-117",
            project_root=self.project_root,
            attachment_root=self.attachment_root,
        )
        self.assertEqual(status["status"], "INCONSISTENT")
        self.assertIn("ORPHAN-FILE", {x["code"] for x in status["anomalies"]})

    def test_t18_target_collision_does_not_overwrite(self) -> None:
        self.matter("MATTER-118")
        source = self.source()
        sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        matter_dir = self.attachment_root / "MATTER-118"
        matter_dir.mkdir(parents=True)
        target = matter_dir / f"MATTER-118__{sha256}.png"
        target.write_bytes(b"different")
        self.assert_error(
            "TARGET-COLLISION",
            lambda: self.capture("MATTER-118", source),
        )
        self.assertEqual(target.read_bytes(), b"different")
        self.assertFalse((matter_dir / "manifest.json").exists())

    def test_t19_list_uses_manifest_not_source_scan_or_hash(self) -> None:
        self.matter("MATTER-119")
        source = self.source("captured.png")
        self.capture("MATTER-119", source)
        self.source("uncaptured.png")
        with mock.patch.object(tool, "hash_file", side_effect=AssertionError("no hash")):
            listing = tool.list_attachments(
                "MATTER-119",
                project_root=self.project_root,
                attachment_root=self.attachment_root,
            )
        self.assertEqual(listing["attachment_count"], 1)
        self.assertEqual(listing["attachments"][0]["item_status"], "RECORDED-COMPLETE")

    def test_t20_status_verifies_consistency_and_sha_mismatch(self) -> None:
        self.matter("MATTER-120")
        source = self.source()
        result = self.capture("MATTER-120", source)
        consistent = tool.status_matter(
            "MATTER-120",
            project_root=self.project_root,
            attachment_root=self.attachment_root,
        )
        self.assertEqual(consistent["status"], "CONSISTENT")
        target = self.attachment_root.joinpath(
            *Path(result["stored_relative_path"]).parts  # type: ignore[arg-type]
        )
        target.write_bytes(os.urandom(target.stat().st_size))
        inconsistent = tool.status_matter(
            "MATTER-120",
            project_root=self.project_root,
            attachment_root=self.attachment_root,
        )
        self.assertIn("SHA-MISMATCH", {x["code"] for x in inconsistent["anomalies"]})

    def test_t21_multiple_attachments_keep_independent_results(self) -> None:
        self.matter("MATTER-121")
        one = self.source("one.png")
        two = self.source("two.png")
        three = self.source("three.txt")
        common = {
            "attribution_confirmed_at": CONFIRMED_AT,
            "source_turn_id": "fictional-same-turn",
        }
        first = self.capture(
            "MATTER-121", one, tool.ATTRIBUTION_MODEL_CONFIRMED, **common
        )
        second = self.capture(
            "MATTER-121", two, tool.ATTRIBUTION_MODEL_CONFIRMED, **common
        )
        self.assert_error(
            "UNSUPPORTED-ATTACHMENT-TYPE",
            lambda: self.capture(
                "MATTER-121", three, tool.ATTRIBUTION_MODEL_CONFIRMED, **common
            ),
        )
        self.assertEqual(first["status"], "FORMAL-ATTACHMENT-CAPTURE-COMPLETE")
        self.assertEqual(second["status"], "FORMAL-ATTACHMENT-CAPTURE-COMPLETE")
        self.assertEqual(len(self.manifest("MATTER-121")["attachments"]), 2)  # type: ignore[arg-type]

    def test_t22_model_confirmed_requires_confirmed_at(self) -> None:
        self.matter("MATTER-122")
        source = self.source()
        self.assert_error(
            "ATTRIBUTION-CONFIRMATION-MISSING",
            lambda: self.capture(
                "MATTER-122", source, tool.ATTRIBUTION_MODEL_CONFIRMED
            ),
        )
        self.assertFalse(self.attachment_root.exists())

    def test_t23_unconfirmed_model_modes_are_rejected(self) -> None:
        self.matter("MATTER-123")
        source = self.source()
        for mode in ("MODEL-MEDIATED", "MODEL-SUPPLIED", "MODEL-MEDIATED-UNCONFIRMED"):
            with self.subTest(mode=mode):
                self.assert_error(
                    "INVALID-ATTRIBUTION-MODE",
                    lambda mode=mode: self.capture("MATTER-123", source, mode),
                )
        self.assertFalse(self.attachment_root.exists())

    def test_t24_confirmed_parameters_are_preserved_mechanically(self) -> None:
        self.matter("MATTER-124")
        source = self.source()
        result = self.capture(
            "MATTER-124",
            source,
            tool.ATTRIBUTION_MODEL_CONFIRMED,
            attribution_confirmed_at=CONFIRMED_AT,
            source_session_id="fictional-session",
            source_message_id="fictional-message",
            source_turn_id="fictional-turn",
            source_attachment_ref="fictional-ref",
            source_filename="fictional.png",
        )
        record = self.manifest("MATTER-124")["attachments"][0]  # type: ignore[index]
        self.assertEqual(result["attribution_mode"], tool.ATTRIBUTION_MODEL_CONFIRMED)
        self.assertEqual(record["attribution_mode"], tool.ATTRIBUTION_MODEL_CONFIRMED)
        self.assertEqual(record["attribution_confirmed_at"], CONFIRMED_AT)
        self.assertEqual(record["source_turn_id"], "fictional-turn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
