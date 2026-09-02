#!/usr/bin/env python3
"""用系统临时目录中的纯虚构文件验证确认归档工具。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_SOURCE = Path(__file__).resolve().with_name("workbench_archive.py")
TARGET_DIRS = (
    "03_居民资料_未入档",
    "05_填写资料模板",
    "06_政策资料",
    "07_会议材料",
    "08_工作材料",
    "09_社区知识",
    "99_无法确认",
)


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_cli(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )


def make_root(temp_name: str) -> tuple[Path, Path]:
    root = Path(temp_name) / "虚构社区工作台"
    script_dir = root / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "workbench_archive.py"
    shutil.copy2(SCRIPT_SOURCE, script)
    (script_dir / "_runtime").mkdir()
    (root / "00_待分类").mkdir()
    for name in TARGET_DIRS:
        (root / name).mkdir()
    return root, script


def write_plan(path: Path, moves: list[dict[str, str]]) -> None:
    """明确以无 BOM UTF-8 写入 TRAE 形态的最小 JSON 计划。"""

    data = json.dumps({"moves": moves}, ensure_ascii=False, indent=2)
    path.write_bytes(data.encode("utf-8"))


def load_module(script: Path):
    spec = importlib.util.spec_from_file_location("workbench_archive_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    results: list[str] = []
    dry_run_output = ""
    execute_output = ""
    with tempfile.TemporaryDirectory(prefix="community-ai-real03-") as temp_name:
        root, script = make_root(temp_name)
        source_ok = root / "00_待分类" / "虚构会议通知.bin"
        source_conflict = root / "00_待分类" / "虚构同名材料.txt"
        target_conflict = root / "08_工作材料" / source_conflict.name
        ok_content = b"REAL-03 fictional binary\x00\xff\x10\r\n"
        source_conflict_content = b"fictional incoming version"
        target_conflict_content = b"fictional existing version"
        write_bytes(source_ok, ok_content)
        write_bytes(source_conflict, source_conflict_content)
        write_bytes(target_conflict, target_conflict_content)

        # A：dry-run 展示准备移动和冲突，整个虚构树一个字节也不改变。
        before_dry_run = tree_snapshot(root)
        proc = run_cli(
            script,
            "--move",
            "00_待分类/虚构会议通知.bin",
            "07_会议材料",
            "--move",
            "00_待分类/虚构同名材料.txt",
            "08_工作材料",
            "--dry-run",
        )
        assert proc.returncode == 3, proc.stdout + proc.stderr
        dry_run_output = proc.stdout.rstrip()
        assert "[准备移动]" in proc.stdout
        assert "[同名冲突]" in proc.stdout
        assert tree_snapshot(root) == before_dry_run
        results.append("A｜PASS｜dry-run 显示 1 项准备移动、1 项同名冲突，全部文件 SHA-256 不变")

        # B：正式执行允许无冲突项成功，同时保留冲突项两侧原文件。
        ok_hash_before = digest(source_ok)
        conflict_source_hash = digest(source_conflict)
        conflict_target_hash = digest(target_conflict)
        proc = run_cli(
            script,
            "--move",
            "00_待分类/虚构会议通知.bin",
            "07_会议材料",
            "--move",
            "00_待分类/虚构同名材料.txt",
            "08_工作材料",
        )
        assert proc.returncode == 3, proc.stdout + proc.stderr
        execute_output = proc.stdout.rstrip()
        moved = root / "07_会议材料" / source_ok.name
        assert not source_ok.exists()
        assert moved.is_file()
        assert moved.name == source_ok.name
        assert digest(moved) == ok_hash_before
        assert source_conflict.is_file() and digest(source_conflict) == conflict_source_hash
        assert target_conflict.is_file() and digest(target_conflict) == conflict_target_hash
        assert "成功 1；同名冲突未移动 1；失败 0" in proc.stdout
        results.append("B｜PASS｜无冲突项成功移动且原名、SHA-256 不变；冲突项源和既有目标均未改变")

        # C：项目范围外、非固定目标目录和重复映射全部拒绝，零移动。
        invalid_source = root / "00_待分类" / "虚构范围校验.txt"
        write_bytes(invalid_source, b"fictional scope validation")
        before_invalid = tree_snapshot(root)
        proc = run_cli(
            script,
            "--move",
            "../虚构范围外.txt",
            "08_工作材料",
            "--move",
            "00_待分类/虚构范围校验.txt",
            "04_居民档案",
            "--move",
            "00_待分类/虚构范围校验.txt",
            "09_社区知识",
            "--move",
            "00_待分类/虚构范围校验.txt",
            "09_社区知识",
        )
        assert proc.returncode == 3
        assert proc.stdout.count("[失败]") == 4
        assert tree_snapshot(root) == before_invalid
        results.append("C｜PASS｜越界源、非固定分类和重复映射均明确失败，未移动任何文件")

        # D：注入单文件移动故障；同批另一项仍成功，故障项保留并明确报告。
        module = load_module(script)
        source_fail = root / "00_待分类" / "虚构移动失败.dat"
        source_second_ok = root / "00_待分类" / "虚构继续成功.dat"
        write_bytes(source_fail, b"fictional move failure")
        write_bytes(source_second_ok, b"fictional second success")
        fail_hash = digest(source_fail)
        second_hash = digest(source_second_ok)
        items = module.prepare_archive(
            root,
            [
                ["00_待分类/虚构移动失败.dat", "06_政策资料"],
                ["00_待分类/虚构继续成功.dat", "09_社区知识"],
            ],
        )

        def fail_one(source: Path, target: Path) -> None:
            if source.name == "虚构移动失败.dat":
                raise OSError("REAL-03 虚构权限故障")
            os.rename(source, target)

        module.execute_archive(root, items, move_file=fail_one)
        failure_report = module.report_text(root, items, dry_run=False)
        assert items[0].status == module.FAILED
        assert "REAL-03 虚构权限故障" in failure_report
        assert source_fail.is_file() and digest(source_fail) == fail_hash
        second_target = root / "09_社区知识" / source_second_ok.name
        assert not source_second_ok.exists()
        assert second_target.is_file() and digest(second_target) == second_hash
        results.append("D｜PASS｜单项移动故障被明确报告且源保留，同批其他已确认文件仍可安全完成")

        # E：全部成功时 CLI 返回 0，供 TRAE 可靠判断“归档完成”。
        source_all_ok = root / "00_待分类" / "虚构全部成功.txt"
        write_bytes(source_all_ok, b"fictional all-success case")
        all_ok_hash = digest(source_all_ok)
        proc = run_cli(
            script,
            "--move",
            "00_待分类/虚构全部成功.txt",
            "05_填写资料模板",
        )
        all_ok_target = root / "05_填写资料模板" / source_all_ok.name
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "成功 1；同名冲突未移动 0；失败 0" in proc.stdout
        assert not source_all_ok.exists()
        assert all_ok_target.is_file() and digest(all_ok_target) == all_ok_hash
        results.append("E｜PASS｜全部成功时 CLI 返回 0；存在冲突或失败的批次返回 3")

        # F：计划文件承载左右引号、空格、中文冒号和长中文文件名的多文件批量 dry-run。
        special_names = (
            "“虚构社区通知” 空格与中文冒号：第一版.docx",
            "‘虚构报名表’ 含多个 空格：第二版.xlsx",
            "虚构的非常长中文文件名称用于验证TRAE不再拼接复杂命令行参数并且能够稳定传递到确定性归档程序：最终版本材料.txt",
        )
        special_targets = ("07_会议材料", "05_填写资料模板", "08_工作材料")
        special_hashes: dict[str, str] = {}
        moves: list[dict[str, str]] = []
        for index, (name, target_name) in enumerate(zip(special_names, special_targets), start=1):
            source = root / "00_待分类" / name
            write_bytes(source, f"fictional special file {index}".encode("utf-8"))
            special_hashes[name] = digest(source)
            moves.append({"source": f"00_待分类/{name}", "target": target_name})

        plan_conflict_name = "“虚构同名冲突” 材料：保留源文件.txt"
        plan_conflict_source = root / "00_待分类" / plan_conflict_name
        plan_conflict_target = root / "09_社区知识" / plan_conflict_name
        write_bytes(plan_conflict_source, b"fictional plan incoming")
        write_bytes(plan_conflict_target, b"fictional plan existing")
        plan_conflict_source_hash = digest(plan_conflict_source)
        plan_conflict_target_hash = digest(plan_conflict_target)
        moves.append(
            {"source": f"00_待分类/{plan_conflict_name}", "target": "09_社区知识"}
        )

        plan = root / "scripts" / "_runtime" / "real03_虚构批量计划.json"
        write_plan(plan, moves)
        plan_hash = digest(plan)
        before_plan_dry_run = tree_snapshot(root)
        proc = run_cli(
            script,
            "--plan-file",
            "scripts/_runtime/real03_虚构批量计划.json",
            "--dry-run",
        )
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert proc.stdout.count("[准备移动]") == 3
        assert proc.stdout.count("[同名冲突]") == 1
        assert "dry-run 保留临时计划文件" in proc.stdout
        assert plan.is_file() and digest(plan) == plan_hash
        assert tree_snapshot(root) == before_plan_dry_run
        results.append("F｜PASS｜UTF-8 计划批量承载左右引号、空格、中文冒号和长中文名；dry-run 零写入并保留计划")

        # G：同一计划正式执行；成功项逐一校验，冲突项保留，计划自动清理。
        proc = run_cli(
            script,
            "--plan-file",
            "scripts/_runtime/real03_虚构批量计划.json",
        )
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "成功 3；同名冲突未移动 1；失败 0" in proc.stdout
        assert "已清理本次临时计划文件" in proc.stdout
        assert not plan.exists()
        for name, target_name in zip(special_names, special_targets):
            source = root / "00_待分类" / name
            target = root / target_name / name
            assert not source.exists()
            assert target.is_file() and target.name == name
            assert digest(target) == special_hashes[name]
        assert plan_conflict_source.is_file()
        assert digest(plan_conflict_source) == plan_conflict_source_hash
        assert plan_conflict_target.is_file()
        assert digest(plan_conflict_target) == plan_conflict_target_hash
        assert not list(root.rglob("_run_archive.py"))
        assert not [path for path in root.rglob("*.py") if "helper" in path.name.lower()]
        results.append("G｜PASS｜正式批量移动 3 项、保留 1 项冲突及两侧内容，并自动清理本次计划文件")

        # H：非 UTF-8 计划被安全拒绝，不执行映射，也不误删无法解析的计划。
        invalid_plan = root / "scripts" / "_runtime" / "real03_非UTF8计划.json"
        invalid_plan.write_bytes(b'{"moves":[{"source":"\xff","target":"08_"}]}')
        proc = run_cli(
            script,
            "--plan-file",
            "scripts/_runtime/real03_非UTF8计划.json",
        )
        assert proc.returncode == 2
        assert "必须使用有效 UTF-8 编码" in proc.stderr
        assert invalid_plan.is_file()
        results.append("H｜PASS｜非 UTF-8 计划在执行前安全中止，未移动文件且保留原计划供诊断")

        # I：即使 JSON 有效，固定运行目录外的计划也不能作为输入或被程序删除。
        outside_plan = root / "scripts" / "real03_目录外计划.json"
        write_plan(
            outside_plan,
            [{"source": "00_待分类/虚构范围校验.txt", "target": "08_工作材料"}],
        )
        outside_hash = digest(outside_plan)
        proc = run_cli(
            script,
            "--plan-file",
            "scripts/real03_目录外计划.json",
        )
        assert proc.returncode == 2
        assert "必须位于 scripts/_runtime/ 内" in proc.stderr
        assert outside_plan.is_file() and digest(outside_plan) == outside_hash
        results.append("I｜PASS｜固定运行目录外的有效 JSON 计划被拒绝且未被程序删除")

    print("=== DRY-RUN ACTUAL OUTPUT ===")
    print(dry_run_output)
    print("=== EXECUTE ACTUAL OUTPUT ===")
    print(execute_output)
    print("=== REAL-03 SCENARIOS ===")
    for result in results:
        print(result)
    print("虚构测试目录已由 TemporaryDirectory 清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
