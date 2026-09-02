#!/usr/bin/env python3
"""社区 AI 工作台 Phase 1：MATTER/RESIDENT ID 与已确认关系的双向关联。

本工具不判断是否建档、是否同一居民或是否同一事项。它只在调用方已经
完成这些判断后，维护一个或两个 Markdown 文件中的固定系统区域。
"""

from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


SYSTEM_HEADING = "## 工作台关联（系统维护）"
MATTER_DIR = "01_事项台账"
RESIDENT_DIR = "04_居民档案"
RESIDENT_REGISTRY_RELATIVE = "scripts/_state/resident_registry.json"
MATTER_ID_RE = re.compile(r"^MATTER-(\d{3,})$")
RESIDENT_ID_RE = re.compile(r"^RESIDENT-(\d{3,})$")
RESIDENT_TOKEN_RE = re.compile(r"RESIDENT-[^\s｜|]+")
NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+.+(?:\r?\n|$)")


class Phase1Error(RuntimeError):
    """可向调用方报告的安全中止。"""


@dataclass(frozen=True)
class Document:
    path: Path
    relative_path: str
    raw: bytes
    text: str
    has_bom: bool
    newline: str
    mode: int

    def encode(self, text: str) -> bytes:
        body = text.encode("utf-8")
        return codecs.BOM_UTF8 + body if self.has_bom else body


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ResidentRegion:
    resident_id: str
    matter_ids: tuple[str, ...]


@dataclass(frozen=True)
class MatterLink:
    resident_id: str
    resident_path: str


@dataclass(frozen=True)
class MatterRegion:
    links: tuple[MatterLink, ...]


@dataclass(frozen=True)
class LinkPlan:
    root: Path
    matter: Document
    resident: Document
    matter_id: str
    resident_id: str
    matter_after: bytes
    resident_after: bytes
    resident_id_created: bool
    matter_link_change: str
    resident_link_change: str

    @property
    def changed(self) -> bool:
        return self.matter.raw != self.matter_after or self.resident.raw != self.resident_after


@dataclass(frozen=True)
class ResidentInitPlan:
    root: Path
    registry: "ResidentRegistry"
    resident: Document
    resident_id: str
    registry_after: bytes
    resident_after: bytes
    resident_id_created: bool
    expected_region: ResidentRegion

    @property
    def changed(self) -> bool:
        return self.registry.raw != self.registry_after or self.resident.raw != self.resident_after


@dataclass(frozen=True)
class ResidentRegistry:
    path: Path
    relative_path: str
    raw: bytes
    entries: dict[str, str]
    mode: int


@dataclass(frozen=True)
class BootstrapCandidate:
    resident_path: str
    resident_id: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class BootstrapPlan:
    root: Path
    registry_path: Path
    entries: dict[str, str]
    registry_after: bytes
    candidate_count: int


def _id_number(identifier: str, pattern: re.Pattern[str]) -> int:
    match = pattern.fullmatch(identifier)
    if not match:
        raise Phase1Error(f"ID 格式异常：{identifier}")
    return int(match.group(1))


def _format_id(prefix: str, number: int) -> str:
    return f"{prefix}-{number:03d}"


def _project_relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_document(root: Path, path: Path) -> Document:
    raw = path.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    body = raw[len(codecs.BOM_UTF8) :] if has_bom else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Phase1Error(f"文件不是可安全处理的 UTF-8 文本：{_project_relative(root, path)}") from exc
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    newline = "\r\n" if crlf_count > lf_count else "\n"
    return Document(
        path=path,
        relative_path=_project_relative(root, path),
        raw=raw,
        text=text,
        has_bom=has_bom,
        newline=newline,
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def _resolve_scoped_file(root: Path, relative: str, required_dir: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise Phase1Error("只接受项目内相对路径，不接受绝对路径。")
    root = root.resolve()
    resolved = (root / candidate).resolve()
    required_root = (root / required_dir).resolve()
    try:
        resolved.relative_to(required_root)
    except ValueError as exc:
        raise Phase1Error(f"文件必须位于 {required_dir}/ 内。") from exc
    if resolved.parent == required_root.parent or resolved.suffix.lower() != ".md":
        raise Phase1Error("目标必须是指定业务目录内的 Markdown 文件。")
    if not resolved.is_file():
        raise Phase1Error(f"目标文件不存在：{relative}")
    return resolved


class _DuplicateJSONKey(ValueError):
    pass


def _resident_registry_path(root: Path) -> Path:
    root = root.resolve()
    path = (root / RESIDENT_REGISTRY_RELATIVE).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Phase1Error("居民 registry 路径必须位于项目根目录内。") from exc
    return path


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _validate_registry_entries(entries: object) -> dict[str, str]:
    if not isinstance(entries, dict):
        raise Phase1Error("居民 registry 顶层必须是“居民档案项目相对路径 → RESIDENT-ID”的 JSON 对象。")
    validated: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for path_text, resident_id in entries.items():
        if not isinstance(path_text, str) or not isinstance(resident_id, str):
            raise Phase1Error("居民 registry 的路径和 RESIDENT-ID 必须都是字符串。")
        if "\\" in path_text:
            raise Phase1Error(f"居民 registry 路径必须使用正斜杠：{path_text}")
        candidate = Path(path_text)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or "." in candidate.parts
            or candidate.as_posix() != path_text
            or not path_text.startswith(f"{RESIDENT_DIR}/")
            or not path_text.lower().endswith(".md")
        ):
            raise Phase1Error(f"居民 registry 包含无效项目相对路径：{path_text}")
        _id_number(resident_id, RESIDENT_ID_RE)
        if resident_id in seen_ids:
            raise Phase1Error(
                f"居民 registry 存在重复 RESIDENT-ID：{resident_id}（{seen_ids[resident_id]}、{path_text}）"
            )
        seen_ids[resident_id] = path_text
        validated[path_text] = resident_id
    return validated


def _parse_registry_bytes(path: Path, raw: bytes) -> dict[str, str]:
    if raw.startswith(codecs.BOM_UTF8):
        raise Phase1Error("居民 registry 必须是无 BOM 的 UTF-8 JSON。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Phase1Error("居民 registry 不是有效 UTF-8。") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJSONKey as exc:
        raise Phase1Error(f"居民 registry 存在重复 JSON key：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise Phase1Error(f"居民 registry JSON 损坏：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    return _validate_registry_entries(parsed)


def load_resident_registry(root: Path) -> ResidentRegistry:
    root = root.resolve()
    path = _resident_registry_path(root)
    if not path.is_file():
        raise Phase1Error(
            f"缺少 RESIDENT-ID 权威 registry：{RESIDENT_REGISTRY_RELATIVE}；"
            "普通命令不得从居民文件自动重建。"
        )
    raw = path.read_bytes()
    entries = _parse_registry_bytes(path, raw)
    return ResidentRegistry(
        path=path,
        relative_path=RESIDENT_REGISTRY_RELATIVE,
        raw=raw,
        entries=entries,
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def render_resident_registry(entries: dict[str, str]) -> bytes:
    validated = _validate_registry_entries(entries)
    return (json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _find_system_section(text: str) -> Optional[Section]:
    heading_re = re.compile(rf"(?m)^{re.escape(SYSTEM_HEADING)}[ \t]*(?:\r?\n|$)")
    matches = list(heading_re.finditer(text))
    if len(matches) > 1:
        raise Phase1Error(f"存在多个“{SYSTEM_HEADING}”区域，拒绝自动修改。")
    if not matches:
        return None
    match = matches[0]
    next_heading = NEXT_HEADING_RE.search(text, match.end())
    end = next_heading.start() if next_heading else len(text)
    return Section(match.start(), end, text[match.start() : end])


def _meaningful_region_lines(section: Section) -> list[str]:
    lines = section.text.splitlines()
    if not lines or lines[0].strip() != SYSTEM_HEADING:
        raise Phase1Error("系统维护关联区标题异常。")
    return [line.strip() for line in lines[1:] if line.strip()]


def _parse_resident_region(text: str) -> Optional[ResidentRegion]:
    section = _find_system_section(text)
    if section is None:
        return None
    lines = _meaningful_region_lines(section)
    if len(lines) < 2:
        raise Phase1Error("居民档案的系统维护关联区不完整。")
    id_lines = [line for line in lines if line.startswith("居民ID：")]
    if len(id_lines) != 1:
        raise Phase1Error("居民档案的系统维护关联区必须且只能有一个居民ID。")
    resident_id = id_lines[0].split("：", 1)[1].strip()
    _id_number(resident_id, RESIDENT_ID_RE)
    if lines.count("关联事项：") != 1:
        raise Phase1Error("居民档案的系统维护关联区必须且只能有一个“关联事项：”。")
    allowed = {id_lines[0], "关联事项："}
    matter_ids: list[str] = []
    for line in lines:
        if line in allowed:
            continue
        if not line.startswith("- "):
            raise Phase1Error(f"居民档案系统维护关联区含有不允许的内容：{line}")
        matter_id = line[2:].strip()
        _id_number(matter_id, MATTER_ID_RE)
        if matter_id in matter_ids:
            raise Phase1Error(f"居民档案系统维护关联区存在重复关联：{matter_id}")
        matter_ids.append(matter_id)
    return ResidentRegion(resident_id, tuple(matter_ids))


def _validate_resident_link_path(path_text: str) -> str:
    if "\\" in path_text:
        raise Phase1Error("系统维护关联区中的居民档案路径必须使用项目相对路径和正斜杠。")
    candidate = Path(path_text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise Phase1Error(f"居民档案关联路径不是安全的项目相对路径：{path_text}")
    if not path_text.startswith(f"{RESIDENT_DIR}/") or not path_text.lower().endswith(".md"):
        raise Phase1Error(f"居民档案关联路径不在 {RESIDENT_DIR}/ 内：{path_text}")
    return path_text


def _parse_matter_region(text: str) -> Optional[MatterRegion]:
    section = _find_system_section(text)
    if section is None:
        return None
    lines = _meaningful_region_lines(section)
    if not lines or lines.count("关联居民：") != 1:
        raise Phase1Error("MATTER 的系统维护关联区必须且只能有一个“关联居民：”。")
    links: list[MatterLink] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for line in lines:
        if line == "关联居民：":
            continue
        if not line.startswith("- ") or "｜" not in line:
            raise Phase1Error(f"MATTER 系统维护关联区含有不允许的内容：{line}")
        resident_id, resident_path = (part.strip() for part in line[2:].split("｜", 1))
        _id_number(resident_id, RESIDENT_ID_RE)
        resident_path = _validate_resident_link_path(resident_path)
        if resident_id in seen_ids or resident_path in seen_paths:
            raise Phase1Error("MATTER 系统维护关联区存在重复或冲突的居民关联。")
        seen_ids.add(resident_id)
        seen_paths.add(resident_path)
        links.append(MatterLink(resident_id, resident_path))
    return MatterRegion(tuple(links))


def _render_resident_region(region: ResidentRegion, newline: str) -> str:
    lines = [SYSTEM_HEADING, "", f"居民ID：{region.resident_id}", "", "关联事项："]
    lines.extend(f"- {matter_id}" for matter_id in region.matter_ids)
    return newline.join(lines) + newline


def _render_matter_region(region: MatterRegion, newline: str) -> str:
    lines = [SYSTEM_HEADING, "", "关联居民："]
    lines.extend(f"- {link.resident_id}｜{link.resident_path}" for link in region.links)
    return newline.join(lines) + newline


def _replace_or_append_region(document: Document, rendered: str) -> bytes:
    section = _find_system_section(document.text)
    if section is not None:
        updated = document.text[: section.start] + rendered + document.text[section.end :]
        return document.encode(updated)
    if not document.text:
        updated = rendered
    elif document.text.endswith(("\n", "\r")):
        updated = document.text + document.newline + rendered
    else:
        updated = document.text + document.newline * 2 + rendered
    return document.encode(updated)


def _matter_id_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for pattern in (
        re.compile(r"(?m)^#\s+(MATTER-[^｜|\s]+)"),
        re.compile(r"(?m)^\|\s*MATTER-ID\s*\|\s*(MATTER-[^|\s]+)\s*\|"),
    ):
        tokens.update(match.group(1) for match in pattern.finditer(text))
    return tokens


def scan_matter_ids(root: Path) -> dict[str, Path]:
    matter_root = root / MATTER_DIR
    if not matter_root.is_dir():
        raise Phase1Error(f"缺少目录：{MATTER_DIR}/")
    found: dict[str, Path] = {}
    anomalies: list[str] = []
    for path in sorted(matter_root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        document = _read_document(root, path)
        filename_match = re.match(r"^(MATTER-[^_\s.]+)", path.name)
        filename_id = filename_match.group(1) if filename_match else None
        body_ids = _matter_id_tokens(document.text)
        candidates = set(body_ids)
        if filename_id:
            candidates.add(filename_id)
        if not filename_id:
            anomalies.append(f"{document.relative_path}：文件名缺少 MATTER-ID")
            continue
        invalid = sorted(item for item in candidates if not MATTER_ID_RE.fullmatch(item))
        if invalid:
            anomalies.append(f"{document.relative_path}：MATTER-ID 格式异常 {', '.join(invalid)}")
            continue
        if not body_ids:
            anomalies.append(f"{document.relative_path}：正文缺少 MATTER-ID")
            continue
        if len(candidates) != 1:
            anomalies.append(f"{document.relative_path}：文件名与正文 MATTER-ID 不一致")
            continue
        matter_id = next(iter(candidates))
        if matter_id in found:
            anomalies.append(
                f"{document.relative_path}：MATTER-ID 与 {_project_relative(root, found[matter_id])} 重复"
            )
            continue
        found[matter_id] = path
    if anomalies:
        raise Phase1Error("发现 MATTER-ID 异常，未作修改：\n- " + "\n- ".join(anomalies))
    return found


def scan_resident_ids(root: Path) -> dict[str, Path]:
    """从居民文件生成派生视图；只用于一致性检查，不是 ID 权威来源。"""
    resident_root = root / RESIDENT_DIR
    if not resident_root.is_dir():
        raise Phase1Error(f"缺少目录：{RESIDENT_DIR}/")
    found: dict[str, Path] = {}
    anomalies: list[str] = []
    for path in sorted(resident_root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        document = _read_document(root, path)
        try:
            region = _parse_resident_region(document.text)
        except Phase1Error as exc:
            anomalies.append(f"{document.relative_path}：{exc}")
            continue
        if region is None:
            continue
        if region.resident_id in found:
            anomalies.append(
                f"{document.relative_path}：RESIDENT-ID 与 "
                f"{_project_relative(root, found[region.resident_id])} 重复"
            )
            continue
        found[region.resident_id] = path
    if anomalies:
        raise Phase1Error("发现 RESIDENT-ID 异常，未作修改：\n- " + "\n- ".join(anomalies))
    return found


def _has_resident_system_state(text: str) -> bool:
    return (
        SYSTEM_HEADING in text
        or "工作台关联" in text
        or "居民ID：" in text
        or "关联事项：" in text
        or RESIDENT_TOKEN_RE.search(text) is not None
    )


def scan_bootstrap_candidates(root: Path) -> list[BootstrapCandidate]:
    root = root.resolve()
    resident_root = root / RESIDENT_DIR
    if not resident_root.is_dir():
        raise Phase1Error(f"缺少目录：{RESIDENT_DIR}/")
    raw_candidates: list[BootstrapCandidate] = []
    resolved_paths: dict[Path, list[str]] = {}
    id_paths: dict[str, list[str]] = {}
    for path in sorted(resident_root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        document = _read_document(root, path)
        if not _has_resident_system_state(document.text):
            continue
        error: Optional[str] = None
        resident_id: Optional[str] = None
        try:
            region = _parse_resident_region(document.text)
            if region is None:
                raise Phase1Error("发现 RESIDENT-ID 痕迹，但不存在完整系统维护区")
            section = _find_system_section(document.text)
            if section is None:
                raise Phase1Error("不存在完整系统维护区")
            outside_region = document.text[: section.start] + document.text[section.end :]
            if _has_resident_system_state(outside_region):
                raise Phase1Error("系统维护区外仍存在 RESIDENT-ID 或系统状态痕迹")
            resident_id = region.resident_id
        except Phase1Error as exc:
            error = str(exc)
        raw_candidates.append(BootstrapCandidate(document.relative_path, resident_id, error))
        resolved_paths.setdefault(path.resolve(), []).append(document.relative_path)
        if resident_id is not None:
            id_paths.setdefault(resident_id, []).append(document.relative_path)

    path_conflicts = {path for paths in resolved_paths.values() if len(paths) > 1 for path in paths}
    id_conflicts = {path for paths in id_paths.values() if len(paths) > 1 for path in paths}
    candidates: list[BootstrapCandidate] = []
    for candidate in raw_candidates:
        errors = [candidate.error] if candidate.error else []
        if candidate.resident_path in path_conflicts:
            errors.append("多个项目相对路径指向同一居民文件")
        if candidate.resident_path in id_conflicts:
            errors.append(f"RESIDENT-ID 冲突：{candidate.resident_id}")
        candidates.append(
            BootstrapCandidate(
                candidate.resident_path,
                candidate.resident_id,
                "；".join(errors) if errors else None,
            )
        )
    return candidates


def prepare_bootstrap_resident_registry(root: Path) -> BootstrapPlan:
    root = root.resolve()
    registry_path = _resident_registry_path(root)
    if registry_path.exists():
        raise Phase1Error("居民 registry 已存在；一次性 bootstrap 创建模式拒绝再次运行。")
    candidates = scan_bootstrap_candidates(root)
    failures = [f"{item.resident_path}：{item.error}" for item in candidates if item.error]
    if failures:
        raise Phase1Error("bootstrap 存在未闭合的异常或冲突，未创建 registry：\n- " + "\n- ".join(failures))
    entries = {
        item.resident_path: item.resident_id
        for item in candidates
        if item.resident_id is not None
    }
    registry_after = render_resident_registry(entries)
    if _parse_registry_bytes(registry_path, registry_after) != entries:
        raise Phase1Error("bootstrap registry 候选内容校验失败。")
    return BootstrapPlan(root, registry_path, entries, registry_after, len(candidates))


def next_matter_id(root: Path) -> str:
    identifiers = scan_matter_ids(root)
    highest = max((_id_number(item, MATTER_ID_RE) for item in identifiers), default=0)
    candidate = _format_id("MATTER", highest + 1)
    while candidate in identifiers:
        highest += 1
        candidate = _format_id("MATTER", highest + 1)
    return candidate


def next_resident_id_from_registry(entries: dict[str, str]) -> str:
    identifiers = set(entries.values())
    highest = max((_id_number(item, RESIDENT_ID_RE) for item in identifiers), default=0)
    candidate = _format_id("RESIDENT", highest + 1)
    while candidate in identifiers:
        highest += 1
        candidate = _format_id("RESIDENT", highest + 1)
    return candidate


def next_resident_id(root: Path) -> str:
    return next_resident_id_from_registry(load_resident_registry(root).entries)


def prepare_init_resident(root: Path, resident_relative: str) -> ResidentInitPlan:
    """准备已由工作人员确认建档、但不要求存在 MATTER 的居民系统初始化。"""

    root = root.resolve()
    resident_path = _resolve_scoped_file(root, resident_relative, RESIDENT_DIR)
    registry = load_resident_registry(root)
    resident = _read_document(root, resident_path)
    registered_id = registry.entries.get(resident.relative_path)

    if registered_id is None:
        if _has_resident_system_state(resident.text):
            raise Phase1Error(
                f"存在未经登记的系统状态，拒绝沿用或自动导入：{resident.relative_path}"
            )
        resident_id = next_resident_id_from_registry(registry.entries)
        expected_region = ResidentRegion(resident_id, ())
        resident_after = _replace_or_append_region(
            resident,
            _render_resident_region(expected_region, resident.newline),
        )
        entries_after = dict(registry.entries)
        entries_after[resident.relative_path] = resident_id
        registry_after = render_resident_registry(entries_after)
        resident_id_created = True
    else:
        resident_id = registered_id
        try:
            resident_region = _parse_resident_region(resident.text)
        except Phase1Error as exc:
            raise Phase1Error(f"已登记居民的系统维护区异常：{resident.relative_path}：{exc}") from exc
        if resident_region is None:
            raise Phase1Error(
                f"已登记居民缺少系统维护区，拒绝自动补写或假定关联事项为空：{resident.relative_path}"
            )
        if resident_region.resident_id != registered_id:
            raise Phase1Error(
                f"居民文件 ID 与 registry 不一致：{resident.relative_path}："
                f"文件为 {resident_region.resident_id}，registry 为 {registered_id}"
            )
        expected_region = resident_region
        resident_after = resident.raw
        registry_after = registry.raw
        resident_id_created = False

    registry_check = _parse_registry_bytes(registry.path, registry_after)
    if registry_check.get(resident.relative_path) != resident_id:
        raise Phase1Error("内存中的居民 registry 候选映射校验失败。")
    resident_check = _parse_resident_region(_decode_like(resident, resident_after))
    if resident_check != expected_region:
        raise Phase1Error("内存中的居民初始化结果校验失败。")
    if resident_id_created and resident_check.matter_ids:
        raise Phase1Error("独立居民初始化不得自动产生 MATTER 关联。")

    return ResidentInitPlan(
        root=root,
        registry=registry,
        resident=resident,
        resident_id=resident_id,
        registry_after=registry_after,
        resident_after=resident_after,
        resident_id_created=resident_id_created,
        expected_region=expected_region,
    )


def prepare_link(root: Path, matter_relative: str, resident_relative: str) -> LinkPlan:
    root = root.resolve()
    matter_path = _resolve_scoped_file(root, matter_relative, MATTER_DIR)
    resident_path = _resolve_scoped_file(root, resident_relative, RESIDENT_DIR)
    registry = load_resident_registry(root)
    matter_ids = scan_matter_ids(root)
    matching_matter_ids = [item for item, path in matter_ids.items() if path.resolve() == matter_path]
    if len(matching_matter_ids) != 1:
        raise Phase1Error("目标 MATTER 文件没有唯一、有效且与文件名一致的 MATTER-ID。")
    matter_id = matching_matter_ids[0]
    matter = _read_document(root, matter_path)
    resident = _read_document(root, resident_path)
    matter_region = _parse_matter_region(matter.text) or MatterRegion(())
    resident_region = _parse_resident_region(resident.text)
    resident_id = registry.entries.get(resident.relative_path)
    if resident_id is None:
        raise Phase1Error(f"居民档案尚未登记到权威 registry：{resident.relative_path}")
    if resident_region is None:
        raise Phase1Error(f"已登记居民缺少系统维护区，拒绝由 link 自动修复：{resident.relative_path}")
    if resident_region.resident_id != resident_id:
        raise Phase1Error(
            f"居民文件 ID 与 registry 不一致：{resident.relative_path}："
            f"文件为 {resident_region.resident_id}，registry 为 {resident_id}"
        )
    resident_id_created = False
    resident_matters = list(resident_region.matter_ids)

    same_path_other_id = [
        link for link in matter_region.links if link.resident_path == resident.relative_path and link.resident_id != resident_id
    ]
    if same_path_other_id:
        raise Phase1Error("目标 MATTER 已用其他 RESIDENT-ID 关联同一居民档案路径，拒绝自动覆盖。")

    new_links: list[MatterLink] = []
    matter_link_change = "无变化（关联已存在）"
    resident_link_change = "无变化（关联已存在）"
    resident_seen = False
    for link in matter_region.links:
        if link.resident_id == resident_id:
            resident_seen = True
            if link.resident_path != resident.relative_path:
                new_links.append(MatterLink(resident_id, resident.relative_path))
                matter_link_change = f"更新 {resident_id} 的档案路径"
            else:
                new_links.append(link)
        else:
            new_links.append(link)
    if not resident_seen:
        new_links.append(MatterLink(resident_id, resident.relative_path))
        matter_link_change = f"新增 {resident_id}"

    if matter_id not in resident_matters:
        resident_matters.append(matter_id)
        resident_link_change = f"新增 {matter_id}"

    new_matter_region = MatterRegion(tuple(new_links))
    new_resident_region = ResidentRegion(resident_id, tuple(resident_matters))
    matter_after = _replace_or_append_region(matter, _render_matter_region(new_matter_region, matter.newline))
    resident_after = _replace_or_append_region(resident, _render_resident_region(new_resident_region, resident.newline))

    # 在任何落盘前，再解析内存结果，确保程序只生成合法的系统维护区。
    matter_check = _parse_matter_region(_decode_like(matter, matter_after))
    resident_check = _parse_resident_region(_decode_like(resident, resident_after))
    if matter_check is None or MatterLink(resident_id, resident.relative_path) not in matter_check.links:
        raise Phase1Error("内存中的 MATTER 关联结果校验失败。")
    if resident_check is None or resident_check.resident_id != resident_id or matter_id not in resident_check.matter_ids:
        raise Phase1Error("内存中的居民关联结果校验失败。")

    return LinkPlan(
        root=root,
        matter=matter,
        resident=resident,
        matter_id=matter_id,
        resident_id=resident_id,
        matter_after=matter_after,
        resident_after=resident_after,
        resident_id_created=resident_id_created,
        matter_link_change=matter_link_change,
        resident_link_change=resident_link_change,
    )


def _decode_like(document: Document, raw: bytes) -> str:
    body = raw[len(codecs.BOM_UTF8) :] if raw.startswith(codecs.BOM_UTF8) else raw
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Phase1Error(f"写入候选内容编码校验失败：{document.relative_path}") from exc


def _write_temp_bytes(target: Path, raw: bytes, mode: int, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.wbp1-", suffix=suffix, dir=target.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        if temp_path.read_bytes() != raw:
            raise Phase1Error(f"临时文件写入后校验失败：{target.name}")
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _check_writable(document: Document) -> None:
    if not os.access(document.path, os.W_OK):
        raise Phase1Error(f"目标文件不可写：{document.relative_path}")
    if not document.mode & stat.S_IWRITE:
        raise Phase1Error(f"目标文件为只读：{document.relative_path}")


def _check_registry_writable(registry: ResidentRegistry) -> None:
    if not os.access(registry.path, os.W_OK):
        raise Phase1Error(f"目标文件不可写：{registry.relative_path}")
    if not registry.mode & stat.S_IWRITE:
        raise Phase1Error(f"目标文件为只读：{registry.relative_path}")


@contextmanager
def workspace_write_lock(root: Path):
    """用一次性独占文件防止两个 Phase 1 写入同时分配同一 RESIDENT-ID。"""
    lock_path = root / ".workbench-phase1.lock"
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise Phase1Error("已有 Phase 1 写入正在执行，或上次异常退出留下锁文件；本次未修改业务文件。") from exc
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)


def commit_bootstrap_resident_registry(
    plan: BootstrapPlan,
    before_replace: Optional[Callable[[Path, Path], None]] = None,
) -> None:
    """一次性原子创建首次居民 registry。"""
    staged: Optional[Path] = None
    replaced = False
    try:
        if plan.registry_path.exists():
            raise Phase1Error("居民 registry 已存在；一次性 bootstrap 创建模式拒绝再次运行。")
        plan.registry_path.parent.mkdir(parents=True, exist_ok=True)
        staged = _write_temp_bytes(plan.registry_path, plan.registry_after, 0o644, ".new")
        if _parse_registry_bytes(plan.registry_path, staged.read_bytes()) != plan.entries:
            raise Phase1Error("bootstrap registry staged 文件校验失败。")
        if plan.registry_path.exists():
            raise Phase1Error("bootstrap 准备期间 registry 已由其他进程创建，已中止。")
        if before_replace is not None:
            before_replace(staged, plan.registry_path)
        os.replace(staged, plan.registry_path)
        replaced = True
        if plan.registry_path.read_bytes() != plan.registry_after:
            raise Phase1Error("bootstrap registry 替换后字节校验失败。")
        final_registry = load_resident_registry(plan.root)
        if final_registry.entries != plan.entries:
            raise Phase1Error("bootstrap registry 提交后映射校验失败。")
    except Exception as exc:
        if replaced:
            try:
                plan.registry_path.unlink(missing_ok=True)
                if plan.registry_path.exists():
                    raise OSError("无法恢复到 registry 不存在的操作前状态")
            except Exception as rollback_exc:
                raise Phase1Error(
                    f"bootstrap 写入失败，且无法移除已创建 registry：{rollback_exc}"
                ) from exc
        if isinstance(exc, Phase1Error):
            raise
        raise Phase1Error(f"bootstrap 写入失败，未创建 registry：{exc}") from exc
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def commit_link(
    plan: LinkPlan,
    before_replace: Optional[Callable[[Path, Path, int], None]] = None,
) -> None:
    """提交两个文件；before_replace 仅供虚构数据故障注入测试。"""
    if not plan.changed:
        return
    documents = (plan.matter, plan.resident)
    expected = {plan.matter.path: plan.matter_after, plan.resident.path: plan.resident_after}
    originals = {document.path: document.raw for document in documents}
    changed = [document for document in documents if expected[document.path] != document.raw]
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    rollback_failures: list[str] = []
    preserve_backups = False
    try:
        # 两侧都先检查，并在各自目录成功生成、回读候选临时文件后才允许替换正式文件。
        for document in documents:
            _check_writable(document)
            stage = _write_temp_bytes(document.path, expected[document.path], document.mode, ".new")
            staged[document.path] = stage
            candidate_text = _decode_like(document, stage.read_bytes())
            if document.path == plan.matter.path:
                _parse_matter_region(candidate_text)
            else:
                _parse_resident_region(candidate_text)
        for document in changed:
            backups[document.path] = _write_temp_bytes(document.path, document.raw, document.mode, ".rollback")
        for document in documents:
            if document.path.read_bytes() != originals[document.path]:
                raise Phase1Error(f"预检后文件被其他进程修改，已中止：{document.relative_path}")

        for ordinal, document in enumerate(changed, start=1):
            # 写第二侧前再次确认尚未被并发修改；若失败会进入下方真实恢复流程。
            if document.path.read_bytes() != originals[document.path]:
                raise Phase1Error(f"提交期间文件被其他进程修改：{document.relative_path}")
            if before_replace is not None:
                before_replace(staged[document.path], document.path, ordinal)
            os.replace(staged[document.path], document.path)
            replaced.append(document.path)
            if document.path.read_bytes() != expected[document.path]:
                raise Phase1Error(f"正式文件替换后校验失败：{document.relative_path}")

        final_matter = _read_document(plan.root, plan.matter.path)
        final_resident = _read_document(plan.root, plan.resident.path)
        matter_region = _parse_matter_region(final_matter.text)
        resident_region = _parse_resident_region(final_resident.text)
        wanted_link = MatterLink(plan.resident_id, plan.resident.relative_path)
        if matter_region is None or wanted_link not in matter_region.links:
            raise Phase1Error("提交后 MATTER 侧关联校验失败。")
        if resident_region is None or plan.matter_id not in resident_region.matter_ids:
            raise Phase1Error("提交后居民侧关联校验失败。")
    except Exception as exc:
        for path in reversed(replaced):
            backup = backups.get(path)
            try:
                if backup is None or not backup.exists():
                    raise OSError("恢复副本不存在")
                os.replace(backup, path)
                if path.read_bytes() != originals[path]:
                    raise OSError("恢复后内容与原始快照不一致")
            except Exception as rollback_exc:  # 极端文件系统故障时保留剩余恢复副本并明确报错。
                rollback_failures.append(f"{_project_relative(plan.root, path)}：{rollback_exc}")
        if rollback_failures:
            preserve_backups = True
            raise Phase1Error(
                "双向写入失败，且自动恢复未完全成功；恢复副本已保留：\n- " + "\n- ".join(rollback_failures)
            ) from exc
        raise Phase1Error(f"双向写入失败，已恢复所有已替换文件：{exc}") from exc
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        if not preserve_backups:
            for path in backups.values():
                path.unlink(missing_ok=True)


def commit_init_resident(
    plan: ResidentInitPlan,
    before_replace: Optional[Callable[[Path, Path], None]] = None,
) -> None:
    """提交 registry 与居民文件；before_replace 仅供虚构数据故障注入测试。"""
    if not plan.changed:
        return
    expected_registry = _parse_registry_bytes(plan.registry.path, plan.registry_after)
    paths = (plan.registry.path, plan.resident.path)
    relatives = {
        plan.registry.path: plan.registry.relative_path,
        plan.resident.path: plan.resident.relative_path,
    }
    originals = {
        plan.registry.path: plan.registry.raw,
        plan.resident.path: plan.resident.raw,
    }
    expected = {
        plan.registry.path: plan.registry_after,
        plan.resident.path: plan.resident_after,
    }
    modes = {
        plan.registry.path: plan.registry.mode,
        plan.resident.path: plan.resident.mode,
    }
    changed = [path for path in paths if originals[path] != expected[path]]
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    preserve_backups: set[Path] = set()
    rollback_failures: list[str] = []
    try:
        _check_registry_writable(plan.registry)
        _check_writable(plan.resident)
        for path in paths:
            staged[path] = _write_temp_bytes(path, expected[path], modes[path], ".new")
        if _parse_registry_bytes(plan.registry.path, staged[plan.registry.path].read_bytes()) != expected_registry:
            raise Phase1Error("居民 registry 候选文件校验失败。")
        candidate_region = _parse_resident_region(
            _decode_like(plan.resident, staged[plan.resident.path].read_bytes())
        )
        if candidate_region != plan.expected_region:
            raise Phase1Error("居民初始化候选文件校验失败。")
        for path in changed:
            backups[path] = _write_temp_bytes(path, originals[path], modes[path], ".rollback")
        for path in paths:
            if path.read_bytes() != originals[path]:
                raise Phase1Error(f"预检后文件被其他进程修改，已中止：{relatives[path]}")
        for path in changed:
            if path.read_bytes() != originals[path]:
                raise Phase1Error(f"提交期间文件被其他进程修改：{relatives[path]}")
            if before_replace is not None:
                before_replace(staged[path], path)
            os.replace(staged[path], path)
            replaced.append(path)
            if path.read_bytes() != expected[path]:
                raise Phase1Error(f"正式文件替换后校验失败：{relatives[path]}")

        final_registry = load_resident_registry(plan.root)
        if final_registry.raw != plan.registry_after:
            raise Phase1Error("提交后居民 registry 完整字节校验失败。")
        if final_registry.entries != expected_registry:
            raise Phase1Error("提交后居民 registry 完整映射校验失败。")
        final_resident = _read_document(plan.root, plan.resident.path)
        if final_resident.raw != plan.resident_after:
            raise Phase1Error("提交后居民档案完整字节校验失败。")
        final_region = _parse_resident_region(final_resident.text)
        if final_region != plan.expected_region:
            raise Phase1Error("提交后居民初始化区域校验失败。")
        if final_registry.entries.get(final_resident.relative_path) != final_region.resident_id:
            raise Phase1Error("提交后 registry 与居民文件路径、RESIDENT-ID 交叉校验失败。")
    except Exception as exc:
        for path in reversed(replaced):
            backup = backups.get(path)
            try:
                if backup is None or not backup.exists():
                    raise OSError("恢复副本不存在")
                os.replace(backup, path)
                if path.read_bytes() != originals[path]:
                    raise OSError("恢复后内容与原始快照不一致")
            except Exception as rollback_exc:
                backup_exists = backup is not None and backup.exists()
                if backup_exists and backup is not None:
                    preserve_backups.add(backup)
                recovery_note = (
                    "恢复副本已保留"
                    if backup_exists
                    else "恢复副本已不存在，正式文件状态需人工核查"
                )
                rollback_failures.append(f"{relatives[path]}：{recovery_note}：{rollback_exc}")
        if rollback_failures:
            raise Phase1Error(
                "居民初始化两文件写入失败，且自动恢复未完全成功：\n- "
                + "\n- ".join(rollback_failures)
            ) from exc
        if replaced:
            raise Phase1Error(f"居民初始化两文件写入失败，已恢复所有已替换文件：{exc}") from exc
        raise Phase1Error(f"居民初始化两文件写入失败，原文件未改变：{exc}") from exc
    finally:
        for path in staged.values():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in backups.values():
            if path not in preserve_backups:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def dry_run_text(plan: LinkPlan) -> str:
    created_line = f"分配 {plan.resident_id}" if plan.resident_id_created else f"沿用 {plan.resident_id}"
    return "\n".join(
        [
            "Phase 1 dry-run（未修改任何文件）",
            "",
            "将修改哪些文件：",
            f"- {plan.matter.relative_path}",
            f"- {plan.resident.relative_path}",
            "",
            "将修改哪个区域：",
            f"- {SYSTEM_HEADING}",
            "",
            "准备产生什么变化：",
            f"{plan.matter_id}：",
            f"- {plan.matter_link_change}",
            f"{plan.resident_id}：",
            f"- {created_line}",
            f"- {plan.resident_link_change}",
            "",
            "不会修改：",
            "- 事项正文",
            "- 来源记录",
            "- 事项状态",
            "- 办理结果",
            "- 居民姓名",
            "- 联系电话",
            "- 地址",
            "- 待办",
        ]
    )


def success_text(plan: LinkPlan) -> str:
    if not plan.changed:
        return (
            f"关联已存在，未改写文件：{plan.resident_id} ↔ {plan.matter_id}\n"
            "重复执行未产生重复条目。"
        )
    return "\n".join(
        [
            f"双向关联完成：{plan.resident_id} ↔ {plan.matter_id}",
            f"- {plan.matter.relative_path}",
            f"- {plan.resident.relative_path}",
            f"仅修改：{SYSTEM_HEADING}",
        ]
    )


def init_resident_dry_run_text(plan: ResidentInitPlan) -> str:
    change = (
        f"分配 {plan.resident_id}，初始化空的“关联事项”"
        if plan.resident_id_created
        else f"沿用 {plan.resident_id}；系统区域已存在，不改写文件"
    )
    registry_change = (
        f"登记 {plan.resident.relative_path} → {plan.resident_id}"
        if plan.resident_id_created
        else "registry 映射已存在且一致，不改写"
    )
    return "\n".join(
        [
            "Phase 1 init-resident dry-run（未修改任何文件）",
            "",
            "目标文件：",
            f"- {plan.registry.relative_path}",
            f"- {plan.resident.relative_path}",
            "",
            "准备处理的唯一区域：",
            f"- {SYSTEM_HEADING}",
            "",
            "准备产生什么变化：",
            f"- {registry_change}",
            f"- {change}",
            "",
            "不会修改或创建：",
            "- 居民档案正文中的姓名、证件、地址或其他业务事实",
            "- MATTER 文件或 MATTER 关联",
            "- 待办及其他业务文件",
        ]
    )


def init_resident_success_text(plan: ResidentInitPlan) -> str:
    if not plan.changed:
        return (
            f"居民档案已完成系统初始化，未改写文件：{plan.resident_id}\n"
            "重复执行未重新分配 ID，也未改变既有关联。"
        )
    return "\n".join(
        [
            f"居民档案系统初始化完成：{plan.resident_id}",
            f"- {plan.registry.relative_path}",
            f"- {plan.resident.relative_path}",
            f"已登记映射并新增：{SYSTEM_HEADING}",
            "关联事项：空（未创建 MATTER）",
        ]
    )


def bootstrap_candidates_text(candidates: list[BootstrapCandidate]) -> str:
    lines = [
        "RESIDENT registry bootstrap 候选（只读，未创建 registry）",
        "以下内容仅为待人工确认迁移候选，不具有权威性。",
        "",
    ]
    if not candidates:
        lines.append("未发现历史迁移候选；明确确认后可创建空 registry：{}")
        return "\n".join(lines)
    for candidate in candidates:
        if candidate.error:
            lines.append(f"- [异常] {candidate.resident_path}：{candidate.error}")
        else:
            lines.append(f"- [待确认] {candidate.resident_path} → {candidate.resident_id}")
    lines.extend(
        [
            "",
            "只有全部迁移对象均合法且工作人员明确确认全部映射后，才允许一次性创建 registry。",
        ]
    )
    return "\n".join(lines)


def bootstrap_success_text(plan: BootstrapPlan) -> str:
    return "\n".join(
        [
            "RESIDENT registry bootstrap 完成。",
            f"- {RESIDENT_REGISTRY_RELATIVE}",
            f"- 已登记映射：{len(plan.entries)}",
            f"- 已闭合迁移对象：{plan.candidate_count}",
            "registry 已成为 RESIDENT-ID 唯一权威来源。",
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="社区 AI 工作台 Phase 1 确定性工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("next-matter-id", help="扫描事项台账并输出下一个 MATTER-ID")
    subparsers.add_parser("next-resident-id", help="根据权威 registry 输出下一个 RESIDENT-ID")
    bootstrap_parser = subparsers.add_parser(
        "bootstrap-resident-registry",
        help="只读列出历史候选或经明确确认后一次性创建居民 registry",
    )
    bootstrap_mode = bootstrap_parser.add_mutually_exclusive_group(required=True)
    bootstrap_mode.add_argument("--list", action="store_true", help="只读列出全部迁移候选")
    bootstrap_mode.add_argument(
        "--confirm-all",
        action="store_true",
        help="确认全部合法候选并一次性创建 registry；无候选时创建 {}",
    )
    init_parser = subparsers.add_parser("init-resident", help="初始化已确认建档的独立居民系统区域")
    init_parser.add_argument("--resident-file", required=True, help="04_居民档案/ 内项目相对路径")
    init_parser.add_argument("--dry-run", action="store_true", help="只读预览，不修改任何文件")
    link_parser = subparsers.add_parser("link", help="执行已确认居民与 MATTER 的双向关联")
    link_parser.add_argument("--matter-file", required=True, help="01_事项台账/ 内项目相对路径")
    link_parser.add_argument("--resident-file", required=True, help="04_居民档案/ 内项目相对路径")
    link_parser.add_argument("--dry-run", action="store_true", help="只读预览，不修改任何文件")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        if args.command == "next-matter-id":
            print(next_matter_id(root))
        elif args.command == "next-resident-id":
            print(next_resident_id(root))
        elif args.command == "bootstrap-resident-registry":
            if args.list:
                if _resident_registry_path(root).exists():
                    raise Phase1Error("居民 registry 已存在；bootstrap 已闭合。")
                print(bootstrap_candidates_text(scan_bootstrap_candidates(root)))
            else:
                with workspace_write_lock(root):
                    plan = prepare_bootstrap_resident_registry(root)
                    commit_bootstrap_resident_registry(plan)
                print(bootstrap_success_text(plan))
        elif args.command == "init-resident":
            if args.dry_run:
                plan = prepare_init_resident(root, args.resident_file)
                print(init_resident_dry_run_text(plan))
            else:
                with workspace_write_lock(root):
                    plan = prepare_init_resident(root, args.resident_file)
                    commit_init_resident(plan)
                print(init_resident_success_text(plan))
        else:
            if args.dry_run:
                plan = prepare_link(root, args.matter_file, args.resident_file)
                print(dry_run_text(plan))
            else:
                # 正式执行从重新扫描 ID 到双文件提交全程持有一次性锁；dry-run 不创建锁文件。
                with workspace_write_lock(root):
                    plan = prepare_link(root, args.matter_file, args.resident_file)
                    commit_link(plan)
                print(success_text(plan))
        return 0
    except Phase1Error as exc:
        print(f"Phase 1 已安全中止：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
