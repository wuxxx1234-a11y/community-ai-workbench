#!/usr/bin/env python3
"""MATTER 图片附件 V1：显式来源、逐附件、可追溯的机械捕获工具。"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import uuid
from typing import Any, Iterator


TOOL_VERSION = "1.0.0"
MANIFEST_VERSION = "1"
MATTER_ID_RE = re.compile(r"^MATTER-(\d{3,})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATTACHMENT_ROOT = Path(r"E:\社区工作\工作台事项附件")
DEFAULT_APPROVED_SOURCE_ROOT = Path(
    r"C:\Users\Administrator\.trae-cn\attachments"
)

ATTRIBUTION_MECHANICAL = "MECHANICAL"
ATTRIBUTION_MODEL_CONFIRMED = "MODEL-MEDIATED-CONFIRMED"
ALLOWED_ATTRIBUTION_MODES = {
    ATTRIBUTION_MECHANICAL,
    ATTRIBUTION_MODEL_CONFIRMED,
}

MEDIA_EXTENSION_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MEDIA_COMPATIBLE_SUFFIXES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
ALLOWED_SOURCE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class AttachmentError(RuntimeError):
    """A stable, machine-identifiable tool boundary failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> AttachmentError:
    return AttachmentError(code, message)


def validate_matter_id(matter_id: str) -> str:
    if MATTER_ID_RE.fullmatch(matter_id or "") is None:
        raise _fail("INVALID-MATTER-ID", "MATTER-ID格式不合法")
    return matter_id


def find_unique_matter(project_root: Path, matter_id: str) -> Path:
    validate_matter_id(matter_id)
    ledger = project_root.resolve() / "01_事项台账"
    if not ledger.is_dir():
        raise _fail("MATTER-NOT-FOUND", "事项台账目录不存在")

    candidates: list[Path] = []
    for path in ledger.iterdir():
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        match = re.match(r"^(MATTER-\d{3,})(?:_|\.|$)", path.name)
        if match and match.group(1) == matter_id:
            candidates.append(path)

    if not candidates:
        raise _fail("MATTER-NOT-FOUND", f"未找到{matter_id}对应事项文件")
    if len(candidates) != 1:
        raise _fail("MATTER-NOT-UNIQUE", f"{matter_id}对应事项文件不唯一")
    return candidates[0]


def validate_attribution(
    attribution_mode: str, attribution_confirmed_at: str | None
) -> str | None:
    if attribution_mode not in ALLOWED_ATTRIBUTION_MODES:
        raise _fail("INVALID-ATTRIBUTION-MODE", "不允许的附件归属模式")

    if attribution_mode == ATTRIBUTION_MODEL_CONFIRMED:
        if not attribution_confirmed_at:
            raise _fail(
                "ATTRIBUTION-CONFIRMATION-MISSING",
                "MODEL-MEDIATED-CONFIRMED缺少attribution-confirmed-at",
            )
        try:
            parsed = dt.datetime.fromisoformat(
                attribution_confirmed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise _fail(
                "ATTRIBUTION-CONFIRMATION-INVALID",
                "attribution-confirmed-at不是合法ISO 8601时间",
            ) from exc
        if parsed.tzinfo is None:
            raise _fail(
                "ATTRIBUTION-CONFIRMATION-INVALID",
                "attribution-confirmed-at必须包含时区",
            )
        return attribution_confirmed_at
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(path)), os.path.normcase(str(root))]
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(root)) and path != root


def resolve_approved_source(source_path: Path, approved_root: Path) -> Path:
    if not source_path.is_absolute():
        raise _fail("SOURCE-PATH-NOT-ABSOLUTE", "source-path必须是绝对路径")
    try:
        resolved_root = approved_root.resolve(strict=True)
    except OSError as exc:
        raise _fail("APPROVED-SOURCE-ROOT-INVALID", "批准来源根目录不可用") from exc
    if not resolved_root.is_dir():
        raise _fail("APPROVED-SOURCE-ROOT-INVALID", "批准来源根不是目录")

    try:
        resolved_source = source_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _fail("SOURCE-NOT-FOUND", "source-path不存在") from exc
    except OSError as exc:
        raise _fail("SOURCE-NOT-READABLE", "source-path无法解析") from exc

    if not _is_within(resolved_source, resolved_root):
        raise _fail(
            "SOURCE-OUTSIDE-APPROVED-ROOT",
            "resolve后的source不在批准TRAE附件根目录内",
        )
    try:
        mode = resolved_source.stat().st_mode
    except OSError as exc:
        raise _fail("SOURCE-NOT-READABLE", "source状态不可读") from exc
    if not stat.S_ISREG(mode):
        raise _fail("SOURCE-NOT-REGULAR-FILE", "source不是普通文件")
    return resolved_source


def determine_extension(source: Path, source_media_type: str | None) -> tuple[str, str | None]:
    suffix = source.suffix.lower()
    media_type = None
    if source_media_type:
        media_type = source_media_type.split(";", 1)[0].strip().lower()
        if media_type not in MEDIA_EXTENSION_MAP:
            raise _fail("UNSUPPORTED-ATTACHMENT-TYPE", "不支持的source media type")
        if suffix and suffix not in ALLOWED_SOURCE_SUFFIXES:
            raise _fail(
                "ATTACHMENT-TYPE-CONFLICT",
                "source media type与source suffix冲突",
            )
        if suffix and suffix not in MEDIA_COMPATIBLE_SUFFIXES[media_type]:
            raise _fail(
                "ATTACHMENT-TYPE-CONFLICT",
                "source media type与source suffix冲突",
            )
        return MEDIA_EXTENSION_MAP[media_type], media_type

    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        raise _fail("UNSUPPORTED-ATTACHMENT-TYPE", "无法安全确定批准的图片类型")
    return (".jpg" if suffix == ".jpeg" else suffix), None


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise _fail("SOURCE-NOT-READABLE", f"无法读取文件字节：{path}") from exc
    return digest.hexdigest(), size


def _validate_relative_path(value: Any, matter_id: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise _fail("CORRUPT-MANIFEST", "stored_relative_path类型错误")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise _fail("CORRUPT-MANIFEST", "stored_relative_path越界")
    if len(relative.parts) != 2 or relative.parts[0] != matter_id:
        raise _fail("CORRUPT-MANIFEST", "stored_relative_path不属于目标MATTER")
    return relative


def _new_manifest(matter_id: str) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "matter_id": matter_id,
        "attachments": [],
    }


def load_manifest(path: Path, matter_id: str) -> dict[str, Any]:
    if not path.exists():
        return _new_manifest(matter_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("CORRUPT-MANIFEST", "manifest无法解析") from exc
    if not isinstance(data, dict):
        raise _fail("CORRUPT-MANIFEST", "manifest顶层不是对象")
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise _fail("UNSUPPORTED-MANIFEST-VERSION", "manifest版本不受支持")
    if data.get("matter_id") != matter_id:
        raise _fail("CORRUPT-MANIFEST", "manifest matter_id不匹配")
    attachments = data.get("attachments")
    if not isinstance(attachments, list):
        raise _fail("CORRUPT-MANIFEST", "manifest attachments不是数组")

    seen: set[str] = set()
    required = {
        "sha256",
        "byte_size",
        "stored_relative_path",
        "captured_at",
        "source_type",
        "source_path_at_capture",
        "source_extension",
        "attribution_mode",
        "capture_status",
        "capture_tool_version",
    }
    for record in attachments:
        if not isinstance(record, dict) or not required.issubset(record):
            raise _fail("CORRUPT-MANIFEST", "manifest附件记录字段不完整")
        sha256 = record.get("sha256")
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise _fail("CORRUPT-MANIFEST", "manifest sha256无效")
        if sha256 in seen:
            raise _fail("CORRUPT-MANIFEST", "manifest存在重复sha256")
        seen.add(sha256)
        if not isinstance(record.get("byte_size"), int) or record["byte_size"] < 0:
            raise _fail("CORRUPT-MANIFEST", "manifest byte_size无效")
        relative = _validate_relative_path(record.get("stored_relative_path"), matter_id)
        expected_prefix = f"{matter_id}__{sha256}"
        if (
            not relative.name.startswith(expected_prefix)
            or relative.name[len(expected_prefix) :].lower()
            not in {".jpg", ".png", ".webp"}
        ):
            raise _fail("CORRUPT-MANIFEST", "manifest正式文件名与SHA不一致")
        if record.get("source_type") != "trae_attachment":
            raise _fail("CORRUPT-MANIFEST", "manifest source_type无效")
        mode = record.get("attribution_mode")
        if mode not in ALLOWED_ATTRIBUTION_MODES:
            raise _fail("CORRUPT-MANIFEST", "manifest attribution_mode无效")
        if mode == ATTRIBUTION_MODEL_CONFIRMED:
            try:
                validate_attribution(mode, record.get("attribution_confirmed_at"))
            except AttachmentError as exc:
                raise _fail("CORRUPT-MANIFEST", "manifest确认时间无效") from exc
        if record.get("capture_status") != "COMPLETE":
            raise _fail("CORRUPT-MANIFEST", "manifest存在非COMPLETE记录")
    return data


def _relative_to_path(root: Path, value: str, matter_id: str) -> Path:
    relative = _validate_relative_path(value, matter_id)
    return root.joinpath(*relative.parts)


def _copy_stream(source: Path, target: Path) -> None:
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
        target_handle.flush()
        os.fsync(target_handle.fileno())


def _formal_paths(
    attachment_root: Path, matter_id: str, *, create: bool
) -> tuple[Path, Path]:
    root_input = attachment_root
    if create:
        root_input.mkdir(parents=True, exist_ok=True)
    root = root_input.resolve()
    matter_dir = root / matter_id
    if create and not matter_dir.exists():
        matter_dir.mkdir()
    if not matter_dir.exists():
        return root, matter_dir
    try:
        resolved_matter_dir = matter_dir.resolve(strict=True)
    except OSError as exc:
        raise _fail("FORMAL-PATH-ESCAPE", "正式MATTER附件目录无法解析") from exc
    if (
        os.path.normcase(str(resolved_matter_dir))
        != os.path.normcase(str(matter_dir))
        or not resolved_matter_dir.is_dir()
    ):
        raise _fail(
            "FORMAL-PATH-ESCAPE",
            "正式MATTER附件目录发生symlink、junction或reparse逃逸",
        )
    return root, resolved_matter_dir


def _place_without_overwrite(staged: Path, target: Path) -> None:
    try:
        os.link(staged, target)
    except FileExistsError:
        raise
    except OSError as exc:
        raise _fail("TARGET-PLACE-FAILED", "正式附件无法原子落位") from exc
    else:
        staged.unlink()


def _write_manifest_atomic(path: Path, data: dict[str, Any]) -> None:
    staged = path.parent / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        payload = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        with staged.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise _fail("MANIFEST-COMMIT-FAILED", "manifest原子提交失败") from exc


@contextlib.contextmanager
def _matter_lock(matter_dir: Path) -> Iterator[None]:
    matter_dir.mkdir(parents=True, exist_ok=True)
    lock_path = matter_dir / ".capture.lock"
    handle = lock_path.open("a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - production is Windows
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _fail("CAPTURE-BUSY", "目标MATTER正在执行附件capture") from exc
        locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - production is Windows
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        with contextlib.suppress(OSError):
            lock_path.unlink()


def list_attachments(
    matter_id: str,
    *,
    project_root: Path = DEFAULT_PROJECT_ROOT,
    attachment_root: Path = DEFAULT_ATTACHMENT_ROOT,
) -> dict[str, Any]:
    find_unique_matter(project_root, matter_id)
    root, matter_dir = _formal_paths(attachment_root, matter_id, create=False)
    manifest_path = matter_dir / "manifest.json"
    manifest = load_manifest(manifest_path, matter_id)
    items: list[dict[str, Any]] = []
    for record in manifest["attachments"]:
        target = _relative_to_path(
            root, record["stored_relative_path"], matter_id
        )
        exists = target.is_file()
        size_matches = exists and target.stat().st_size == record["byte_size"]
        item = {
            key: record[key]
            for key in (
                "sha256",
                "byte_size",
                "stored_relative_path",
                "captured_at",
                "attribution_mode",
                "capture_status",
            )
        }
        item["file_exists"] = exists
        item["size_matches"] = bool(size_matches)
        item["item_status"] = (
            "RECORDED-COMPLETE"
            if exists and size_matches
            else "MISSING-FORMAL-FILE"
            if not exists
            else "SIZE-MISMATCH"
        )
        items.append(item)
    return {
        "status": "LIST-COMPLETE",
        "matter_id": matter_id,
        "attachment_count": len(items),
        "attachments": items,
    }


def status_matter(
    matter_id: str,
    *,
    project_root: Path = DEFAULT_PROJECT_ROOT,
    attachment_root: Path = DEFAULT_ATTACHMENT_ROOT,
) -> dict[str, Any]:
    find_unique_matter(project_root, matter_id)
    root, matter_dir = _formal_paths(attachment_root, matter_id, create=False)
    manifest_path = matter_dir / "manifest.json"
    if not matter_dir.exists():
        return {
            "status": "CONSISTENT",
            "matter_id": matter_id,
            "attachment_count": 0,
            "anomalies": [],
        }

    try:
        manifest = load_manifest(manifest_path, matter_id)
    except AttachmentError as exc:
        return {
            "status": exc.code,
            "matter_id": matter_id,
            "attachment_count": 0,
            "anomalies": [{"code": exc.code, "message": exc.message}],
        }

    anomalies: list[dict[str, Any]] = []
    recorded_paths: set[Path] = set()
    for record in manifest["attachments"]:
        target = _relative_to_path(root, record["stored_relative_path"], matter_id)
        recorded_paths.add(target)
        if not target.is_file():
            anomalies.append(
                {"code": "MISSING-FORMAL-FILE", "path": record["stored_relative_path"]}
            )
            continue
        try:
            target_sha, target_size = hash_file(target)
        except AttachmentError:
            anomalies.append(
                {"code": "FORMAL-FILE-NOT-READABLE", "path": record["stored_relative_path"]}
            )
            continue
        if target_size != record["byte_size"]:
            anomalies.append(
                {"code": "SIZE-MISMATCH", "path": record["stored_relative_path"]}
            )
        if target_sha != record["sha256"]:
            anomalies.append(
                {"code": "SHA-MISMATCH", "path": record["stored_relative_path"]}
            )

    for path in matter_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == "manifest.json" or path.name == ".capture.lock":
            continue
        if path.name.startswith("."):
            continue
        if path not in recorded_paths:
            anomalies.append(
                {
                    "code": "ORPHAN-FILE",
                    "path": f"{matter_id}/{path.name}",
                }
            )

    return {
        "status": "CONSISTENT" if not anomalies else "INCONSISTENT",
        "matter_id": matter_id,
        "attachment_count": len(manifest["attachments"]),
        "anomalies": anomalies,
    }


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    if "\x00" in value or len(value) > 4096:
        raise _fail("INVALID-SOURCE-METADATA", "source metadata无效")
    return value


def capture_attachment(
    matter_id: str,
    source_path: Path,
    attribution_mode: str,
    *,
    attribution_confirmed_at: str | None = None,
    source_session_id: str | None = None,
    source_message_id: str | None = None,
    source_turn_id: str | None = None,
    source_attachment_ref: str | None = None,
    source_media_type: str | None = None,
    source_filename: str | None = None,
    project_root: Path = DEFAULT_PROJECT_ROOT,
    attachment_root: Path = DEFAULT_ATTACHMENT_ROOT,
    approved_source_root: Path = DEFAULT_APPROVED_SOURCE_ROOT,
) -> dict[str, Any]:
    find_unique_matter(project_root, matter_id)
    confirmed_at = validate_attribution(
        attribution_mode, attribution_confirmed_at
    )
    source = resolve_approved_source(source_path, approved_source_root)
    formal_extension, normalized_media_type = determine_extension(
        source, source_media_type
    )

    source_session_id = _optional_value(source_session_id)
    source_message_id = _optional_value(source_message_id)
    source_turn_id = _optional_value(source_turn_id)
    source_attachment_ref = _optional_value(source_attachment_ref)
    source_filename = _optional_value(source_filename)

    root, matter_dir = _formal_paths(attachment_root, matter_id, create=True)
    manifest_path = matter_dir / "manifest.json"

    with _matter_lock(matter_dir):
        source_sha, source_size = hash_file(source)
        manifest = load_manifest(manifest_path, matter_id)
        formal_name = f"{matter_id}__{source_sha}{formal_extension}"
        stored_relative_path = PurePosixPath(matter_id, formal_name).as_posix()
        target = matter_dir / formal_name

        existing = next(
            (
                item
                for item in manifest["attachments"]
                if item["sha256"] == source_sha
            ),
            None,
        )
        if existing is not None:
            if existing["byte_size"] != source_size:
                raise _fail("CORRUPT-MANIFEST", "幂等记录的byte_size不一致")
            stored_relative_path = existing["stored_relative_path"]
            target = _relative_to_path(root, stored_relative_path, matter_id)
            if target.is_file():
                target_sha, target_size = hash_file(target)
                if target_sha != source_sha or target_size != source_size:
                    raise _fail("TARGET-COLLISION", "既有正式目标与记录SHA不一致")
                return {
                    "status": "ALREADY-CAPTURED",
                    "matter_id": matter_id,
                    "sha256": source_sha,
                    "byte_size": source_size,
                    "stored_relative_path": stored_relative_path,
                    "captured_at": existing["captured_at"],
                }

        if target.exists():
            if not target.is_file():
                raise _fail("TARGET-COLLISION", "正式目标路径不是普通文件")
            target_sha, target_size = hash_file(target)
            if target_sha != source_sha or target_size != source_size:
                raise _fail("TARGET-COLLISION", "正式目标路径已存在不同内容")
        else:
            staged = matter_dir / f".{formal_name}.{uuid.uuid4().hex}.tmp"
            try:
                try:
                    _copy_stream(source, staged)
                except AttachmentError:
                    raise
                except OSError as exc:
                    raise _fail("COPY-FAILED", "source复制失败") from exc
                target_sha, target_size = hash_file(staged)
                if target_sha != source_sha or target_size != source_size:
                    raise _fail(
                        "TARGET-SHA-MISMATCH",
                        "复制后的target SHA或byte_size与source不一致",
                    )
                try:
                    _place_without_overwrite(staged, target)
                except FileExistsError:
                    target_sha, target_size = hash_file(target)
                    if target_sha != source_sha or target_size != source_size:
                        raise _fail("TARGET-COLLISION", "并发出现不同正式目标")
            finally:
                with contextlib.suppress(OSError):
                    staged.unlink()

        if existing is None:
            record = {
                "sha256": source_sha,
                "byte_size": source_size,
                "stored_relative_path": stored_relative_path,
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "source_type": "trae_attachment",
                "source_path_at_capture": str(source),
                "source_extension": source.suffix.lower(),
                "source_media_type": normalized_media_type,
                "source_filename": source_filename,
                "source_session_id": source_session_id,
                "source_message_id": source_message_id,
                "source_turn_id": source_turn_id,
                "source_attachment_ref": source_attachment_ref,
                "attribution_mode": attribution_mode,
                "attribution_confirmed_at": confirmed_at,
                "capture_status": "COMPLETE",
                "capture_tool_version": TOOL_VERSION,
            }
            manifest["attachments"].append(record)
            manifest["attachments"].sort(key=lambda item: item["sha256"])
            _write_manifest_atomic(manifest_path, manifest)

        reread = load_manifest(manifest_path, matter_id)
        if not any(item["sha256"] == source_sha for item in reread["attachments"]):
            raise _fail("POST-COMMIT-INCONSISTENT", "manifest重读未找到附件记录")
        listed = list_attachments(
            matter_id,
            project_root=project_root,
            attachment_root=root,
        )
        if not any(item["sha256"] == source_sha for item in listed["attachments"]):
            raise _fail("POST-COMMIT-INCONSISTENT", "list未找到附件记录")
        status = status_matter(
            matter_id,
            project_root=project_root,
            attachment_root=root,
        )
        if status["status"] != "CONSISTENT":
            raise _fail("POST-COMMIT-INCONSISTENT", "status未通过一致性检查")

        record = next(
            item for item in reread["attachments"] if item["sha256"] == source_sha
        )
        return {
            "status": "FORMAL-ATTACHMENT-CAPTURE-COMPLETE",
            "matter_id": matter_id,
            "sha256": source_sha,
            "byte_size": source_size,
            "stored_relative_path": stored_relative_path,
            "captured_at": record["captured_at"],
            "attribution_mode": record["attribution_mode"],
        }


def _add_roots(parser: argparse.ArgumentParser, *, capture: bool = False) -> None:
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--attachment-root", type=Path, default=DEFAULT_ATTACHMENT_ROOT)
    if capture:
        parser.add_argument(
            "--approved-source-root",
            type=Path,
            default=DEFAULT_APPROVED_SOURCE_ROOT,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MATTER图片附件V1机械捕获工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="捕获一个显式来源附件")
    capture.add_argument("--matter-id", required=True)
    capture.add_argument("--source-path", required=True, type=Path)
    capture.add_argument("--attribution-mode", required=True)
    capture.add_argument("--attribution-confirmed-at")
    capture.add_argument("--source-session-id")
    capture.add_argument("--source-message-id")
    capture.add_argument("--source-turn-id")
    capture.add_argument("--source-attachment-ref")
    capture.add_argument("--source-media-type")
    capture.add_argument("--source-filename")
    _add_roots(capture, capture=True)

    listing = subparsers.add_parser("list", help="列出某MATTER的正式附件记录")
    listing.add_argument("--matter-id", required=True)
    _add_roots(listing)

    status = subparsers.add_parser("status", help="检查某MATTER附件一致性")
    status.add_argument("--matter-id", required=True)
    _add_roots(status)
    return parser


def _print_json(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capture":
            result = capture_attachment(
                args.matter_id,
                args.source_path,
                args.attribution_mode,
                attribution_confirmed_at=args.attribution_confirmed_at,
                source_session_id=args.source_session_id,
                source_message_id=args.source_message_id,
                source_turn_id=args.source_turn_id,
                source_attachment_ref=args.source_attachment_ref,
                source_media_type=args.source_media_type,
                source_filename=args.source_filename,
                project_root=args.project_root,
                attachment_root=args.attachment_root,
                approved_source_root=args.approved_source_root,
            )
            _print_json(result)
            return 0
        if args.command == "list":
            result = list_attachments(
                args.matter_id,
                project_root=args.project_root,
                attachment_root=args.attachment_root,
            )
            _print_json(result)
            return 0
        result = status_matter(
            args.matter_id,
            project_root=args.project_root,
            attachment_root=args.attachment_root,
        )
        _print_json(result)
        return 0 if result["status"] == "CONSISTENT" else 3
    except AttachmentError as exc:
        _print_json(
            {
                "status": "ATTACHMENT-CAPTURE-INCOMPLETE",
                "error_code": exc.code,
                "message": exc.message,
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
