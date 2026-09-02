#!/usr/bin/env python3
"""执行工作人员已经确认的待分类文件归档。

本工具不判断文件分类。调用方必须逐项提供已经确认的“源文件 -> 目标分类目录”
对应关系；工具只做范围校验、同名冲突保护、移动和内容完整性校验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence


PENDING_DIR = "00_待分类"
RUNTIME_DIR = "scripts/_runtime"
ALLOWED_TARGET_DIRS = (
    "03_居民资料_未入档",
    "05_填写资料模板",
    "06_政策资料",
    "07_会议材料",
    "08_工作材料",
    "09_社区知识",
    "99_无法确认",
)
READY = "准备移动"
SUCCESS = "成功"
CONFLICT = "同名冲突"
FAILED = "失败"


@dataclass
class ArchiveItem:
    """一项已确认移动及其当前处理结果。"""

    source_input: str
    target_dir_input: str
    status: str
    message: str
    source: Optional[Path] = None
    target: Optional[Path] = None
    sha256: Optional[str] = None


@dataclass(frozen=True)
class LoadedPlan:
    """已经严格读取的本次 UTF-8 JSON 归档计划。"""

    path: Path
    relative_path: str
    raw_sha256: str
    moves: tuple[tuple[str, str], ...]


class PlanFileError(RuntimeError):
    """计划文件不符合确定性接口约束。"""


def _relative_text(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_exists(path: Path) -> bool:
    """同时把断开的符号链接视为已占用路径。"""

    return os.path.lexists(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_plan_path(root: Path, plan_file_input: str) -> tuple[Path, str]:
    candidate = Path(plan_file_input)
    if candidate.is_absolute():
        raise PlanFileError("计划文件只接受项目内相对路径，不接受绝对路径")
    root = root.resolve()
    runtime_entry = root / Path(RUNTIME_DIR)
    if runtime_entry.is_symlink() or not runtime_entry.is_dir():
        raise PlanFileError(f"固定运行目录不存在或不安全：{RUNTIME_DIR}/")
    runtime_root = runtime_entry.resolve()
    if runtime_root != runtime_entry:
        raise PlanFileError(f"固定运行目录不能通过链接或重解析点指向其他位置：{RUNTIME_DIR}/")
    plan_entry = root / candidate
    if plan_entry.is_symlink():
        raise PlanFileError("计划文件不能是符号链接")
    plan_path = plan_entry.resolve()
    try:
        plan_path.relative_to(runtime_root)
    except ValueError as exc:
        raise PlanFileError(f"计划文件必须位于 {RUNTIME_DIR}/ 内") from exc
    if plan_path.parent != runtime_root:
        raise PlanFileError(f"计划文件必须直接位于 {RUNTIME_DIR}/ 内")
    if plan_path.suffix.lower() != ".json":
        raise PlanFileError("计划文件扩展名必须是 .json")
    if not plan_path.is_file():
        raise PlanFileError("计划文件不存在或不是普通文件")
    return plan_path, _relative_text(root, plan_path)


def load_plan_file(root: Path, plan_file_input: str) -> LoadedPlan:
    """以严格 UTF-8 读取最小 JSON，并返回调用方明确提供的映射。"""

    root = root.resolve()
    plan_path, relative_path = _validate_plan_path(root, plan_file_input)
    try:
        raw = plan_path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanFileError("计划文件必须使用有效 UTF-8 编码") from exc
    except OSError as exc:
        raise PlanFileError(f"读取计划文件失败：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanFileError(f"计划文件不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    if not isinstance(data, dict) or set(data) != {"moves"}:
        raise PlanFileError('计划文件根对象必须且只能包含 "moves"')
    moves_data = data["moves"]
    if not isinstance(moves_data, list) or not moves_data:
        raise PlanFileError('"moves" 必须是非空数组')
    moves: list[tuple[str, str]] = []
    for index, move in enumerate(moves_data, start=1):
        if not isinstance(move, dict) or set(move) != {"source", "target"}:
            raise PlanFileError(
                f'第 {index} 项必须且只能包含字符串字段 "source" 和 "target"'
            )
        source = move["source"]
        target = move["target"]
        if not isinstance(source, str) or not source:
            raise PlanFileError(f'第 {index} 项的 "source" 必须是非空字符串')
        if not isinstance(target, str) or not target:
            raise PlanFileError(f'第 {index} 项的 "target" 必须是非空字符串')
        moves.append((source, target))
    return LoadedPlan(plan_path, relative_path, hashlib.sha256(raw).hexdigest(), tuple(moves))


def cleanup_plan_file(root: Path, plan: LoadedPlan) -> None:
    """只删除本次已加载且未被替换的临时计划文件。"""

    current_path, current_relative = _validate_plan_path(root, plan.relative_path)
    if current_path != plan.path or current_relative != plan.relative_path:
        raise PlanFileError("清理前计划文件路径发生变化，已拒绝删除")
    try:
        current_hash = _sha256(current_path)
    except OSError as exc:
        raise PlanFileError(f"清理前无法校验计划文件：{exc}") from exc
    if current_hash != plan.raw_sha256:
        raise PlanFileError("计划文件内容在执行期间发生变化，已拒绝删除")
    try:
        current_path.unlink()
    except OSError as exc:
        raise PlanFileError(f"临时计划文件清理失败：{exc}") from exc


def _validate_relative_source(root: Path, source_input: str) -> Path:
    candidate = Path(source_input)
    if candidate.is_absolute():
        raise ValueError("源文件只接受项目相对路径，不接受绝对路径")
    root = root.resolve()
    pending_entry = root / PENDING_DIR
    if pending_entry.is_symlink() or not pending_entry.is_dir():
        raise ValueError(f"{PENDING_DIR}/ 不存在或不是安全的普通目录")
    pending_root = pending_entry.resolve()
    if pending_root != pending_entry:
        raise ValueError(f"{PENDING_DIR}/ 不能通过链接或重解析点指向其他位置")
    source_entry = root / candidate
    if source_entry.is_symlink():
        raise ValueError("源文件不能是符号链接")
    source = source_entry.resolve()
    try:
        source.relative_to(pending_root)
    except ValueError as exc:
        raise ValueError(f"源文件必须位于 {PENDING_DIR}/ 内") from exc
    if source.parent != pending_root:
        raise ValueError(f"源文件必须直接位于 {PENDING_DIR}/ 内")
    if not source.is_file():
        raise ValueError("源文件不存在或不是普通文件")
    return source


def _validate_target_dir(root: Path, target_dir_input: str) -> Path:
    candidate = Path(target_dir_input)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise ValueError("目标目录必须是固定分类表中的项目根目录相对名称")
    name = candidate.parts[0]
    if name not in ALLOWED_TARGET_DIRS:
        raise ValueError("目标目录不在固定分类表中")
    target_dir = root.resolve() / name
    if target_dir.is_symlink():
        raise ValueError("目标分类目录不能是符号链接")
    if not target_dir.is_dir():
        raise ValueError(f"目标分类目录不存在：{name}/")
    if target_dir.resolve() != target_dir:
        raise ValueError("目标分类目录解析结果异常")
    return target_dir


def prepare_archive(root: Path, moves: Sequence[Sequence[str]]) -> list[ArchiveItem]:
    """只读检查调用方提供的映射，绝不推断或修改分类。"""

    root = root.resolve()
    items: list[ArchiveItem] = []
    for move in moves:
        source_input, target_dir_input = move
        try:
            source = _validate_relative_source(root, source_input)
            target_dir = _validate_target_dir(root, target_dir_input)
            target = target_dir / source.name
            if _path_exists(target):
                items.append(
                    ArchiveItem(
                        source_input,
                        target_dir_input,
                        CONFLICT,
                        "目标目录已存在同名文件；未移动，不覆盖、不自动改名",
                        source,
                        target,
                    )
                )
            else:
                items.append(
                    ArchiveItem(
                        source_input,
                        target_dir_input,
                        READY,
                        "已通过范围和同名检查",
                        source,
                        target,
                    )
                )
        except (OSError, ValueError) as exc:
            items.append(ArchiveItem(source_input, target_dir_input, FAILED, str(exc)))

    # 同一批次内的重复源或重复目标不是两次确认动作：所有相关项均拒绝移动。
    source_counts: dict[Path, int] = {}
    target_counts: dict[Path, int] = {}
    for item in items:
        if item.source is not None:
            source_counts[item.source] = source_counts.get(item.source, 0) + 1
        if item.target is not None:
            target_counts[item.target] = target_counts.get(item.target, 0) + 1
    for item in items:
        if item.source is not None and source_counts[item.source] > 1:
            item.status = FAILED
            item.message = "同一批次重复提供了该源文件；未移动"
        elif item.target is not None and target_counts[item.target] > 1:
            item.status = FAILED
            item.message = "同一批次有多个映射指向同一目标文件；未移动"
    return items


def execute_archive(
    root: Path,
    items: list[ArchiveItem],
    move_file: Callable[[Path, Path], None] = os.rename,
) -> list[ArchiveItem]:
    """逐项移动准备就绪的文件，并验证源消失、目标存在、SHA-256 不变。"""

    root = root.resolve()
    for item in items:
        if item.status != READY or item.source is None or item.target is None:
            continue
        source = item.source
        target = item.target
        try:
            # 正式移动前重新检查，避免 dry-run/准备阶段之后的状态变化导致覆盖。
            source = _validate_relative_source(root, _relative_text(root, source))
            target_dir = _validate_target_dir(root, item.target_dir_input)
            expected_target = target_dir / source.name
            if expected_target != target:
                raise OSError("目标路径在执行前发生异常变化")
            if _path_exists(target):
                item.status = CONFLICT
                item.message = "执行时发现目标目录已有同名文件；未移动，不覆盖、不自动改名"
                continue

            before_hash = _sha256(source)
            move_file(source, target)

            if _path_exists(source):
                raise OSError("移动后源文件仍然存在")
            if target.is_symlink() or not target.is_file():
                raise OSError("移动后目标文件不存在或不是普通文件")
            after_hash = _sha256(target)
            if after_hash != before_hash:
                raise OSError("移动后文件 SHA-256 与移动前不一致")

            item.status = SUCCESS
            item.sha256 = after_hash
            item.message = "源文件已不存在，目标文件存在，内容 SHA-256 一致"
        except FileExistsError:
            item.status = CONFLICT
            item.message = "移动时发生同名冲突；未覆盖、未自动改名"
        except OSError as exc:
            item.status = FAILED
            item.message = f"移动或完整性验证失败：{exc}"
    return items


def report_text(root: Path, items: Sequence[ArchiveItem], dry_run: bool) -> str:
    root = root.resolve()
    title = "确认归档 dry-run（未修改任何文件）" if dry_run else "确认归档执行报告"
    lines = [title, ""]
    for index, item in enumerate(items, start=1):
        source_text = (
            _relative_text(root, item.source) if item.source is not None else item.source_input
        )
        target_text = (
            _relative_text(root, item.target)
            if item.target is not None
            else f"{item.target_dir_input}/{Path(item.source_input).name}"
        )
        lines.append(f"{index}. [{item.status}] {source_text} -> {target_text}")
        lines.append(f"   {item.message}")
        if item.sha256 is not None:
            lines.append(f"   SHA-256: {item.sha256}")

    counts = {status: 0 for status in (READY, SUCCESS, CONFLICT, FAILED)}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    lines.extend(["", "汇总："])
    if dry_run:
        lines.append(
            f"- 准备移动 {counts[READY]}；同名冲突 {counts[CONFLICT]}；无法执行 {counts[FAILED]}"
        )
        lines.append("- dry-run 未移动、删除、改名、覆盖或修改任何文件")
    else:
        lines.append(
            f"- 成功 {counts[SUCCESS]}；同名冲突未移动 {counts[CONFLICT]}；失败 {counts[FAILED]}"
        )
        lines.append("- 仅执行已提供的文件—目标目录对应关系；未触发任何业务层写入")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="社区 AI 工作台已确认分类的确定性归档工具")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--move",
        action="append",
        nargs=2,
        metavar=("SOURCE", "TARGET_DIR"),
        help="已确认的项目相对源文件和固定目标分类目录；可重复提供",
    )
    inputs.add_argument(
        "--plan-file",
        help=f"{RUNTIME_DIR}/ 内的 UTF-8 JSON 归档计划项目相对路径",
    )
    parser.add_argument("--dry-run", action="store_true", help="只读预览，不修改任何文件")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    plan: Optional[LoadedPlan] = None
    try:
        if args.plan_file is not None:
            plan = load_plan_file(root, args.plan_file)
            moves: Sequence[Sequence[str]] = plan.moves
        else:
            moves = args.move
    except PlanFileError as exc:
        print(f"确认归档已安全中止：{exc}", file=sys.stderr)
        return 2

    items = prepare_archive(root, moves)
    cleanup_error: Optional[PlanFileError] = None
    if not args.dry_run:
        try:
            execute_archive(root, items)
        finally:
            if plan is not None:
                try:
                    cleanup_plan_file(root, plan)
                except PlanFileError as exc:
                    cleanup_error = exc
    output = report_text(root, items, args.dry_run)
    if plan is not None:
        if args.dry_run:
            output += f"\n- dry-run 保留临时计划文件：{plan.relative_path}"
        elif cleanup_error is None:
            output += f"\n- 已清理本次临时计划文件：{plan.relative_path}"
        else:
            output += f"\n- {cleanup_error}"
    print(output)
    if cleanup_error is not None:
        return 4
    if args.dry_run:
        return 0 if all(item.status == READY for item in items) else 3
    return 0 if all(item.status == SUCCESS for item in items) else 3


if __name__ == "__main__":
    raise SystemExit(main())
