#!/usr/bin/env python3
"""用系统临时目录中的纯虚构数据验证 Phase 1 六个必测场景。"""

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
from unittest import mock


SCRIPT_SOURCE = Path(__file__).resolve().with_name("workbench_phase1.py")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_registry(root: Path, entries: dict[str, str]) -> Path:
    path = root / "scripts" / "_state" / "resident_registry.json"
    write(path, json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


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


def matter_text(identifier: str, title: str) -> str:
    return f"""# {identifier}｜{title}

## 基本信息

| 项目 | 内容 |
|------|------|
| MATTER-ID | {identifier} |
| 来源 | [工作人员] Phase 1 虚构测试 |
| 状态 | 虚构测试状态 |

## 事项正文

这是完全虚构的测试事项，不对应任何真实居民或真实业务。
"""


def resident_text(name: str, identifier: str | None = None, matter_id: str | None = None) -> str:
    base = f"""# {name}

## 基本信息

| 项目 | 内容 | 来源 |
|------|------|------|
| 姓名 | {name} | Phase 1 虚构测试 |
| 地址 | 虚构地址 | Phase 1 虚构测试 |
"""
    if identifier is None:
        return base
    matters = f"- {matter_id}\n" if matter_id else ""
    return base + f"""
## 工作台关联（系统维护）

居民ID：{identifier}

关联事项：
{matters}"""


def main() -> int:
    results: list[str] = []
    dry_run_output = ""
    init_dry_run_output = ""
    with tempfile.TemporaryDirectory(prefix="community-ai-phase1-") as temp_name:
        root = Path(temp_name) / "虚构工作台"
        script_dir = root / "scripts"
        script_dir.mkdir(parents=True)
        script = script_dir / "workbench_phase1.py"
        shutil.copy2(SCRIPT_SOURCE, script)
        matter_dir = root / "01_事项台账"
        resident_dir = root / "04_居民档案"
        write(matter_dir / "README.md", "# 虚构事项目录\n")
        write(resident_dir / "README.md", "# 虚构居民目录\n")
        write(matter_dir / "MATTER-901_虚构既有事项.md", matter_text("MATTER-901", "虚构既有事项一"))
        write(matter_dir / "MATTER-903_虚构既有事项.md", matter_text("MATTER-903", "虚构既有事项二"))
        write(resident_dir / "虚构居民901.md", resident_text("虚构居民901", "RESIDENT-901", "MATTER-901"))
        write(resident_dir / "虚构居民903.md", resident_text("虚构居民903", "RESIDENT-903", "MATTER-903"))
        write_registry(
            root,
            {
                "04_居民档案/虚构居民901.md": "RESIDENT-901",
                "04_居民档案/虚构居民903.md": "RESIDENT-903",
            },
        )

        # A：扫描现有 MATTER 后取最大号 + 1。
        proc = run_cli(script, "next-matter-id")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "MATTER-904", proc.stdout
        results.append("A｜PASS｜现有 MATTER-901、MATTER-903，生成 MATTER-904")

        # B：只根据权威 registry 中的 RESIDENT-ID 取最大号 + 1。
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "RESIDENT-904", proc.stdout
        results.append("B｜PASS｜registry 已登记 RESIDENT-901、RESIDENT-903，生成 RESIDENT-904")

        target_matter = matter_dir / "MATTER-904_虚构新事项.md"
        target_resident = resident_dir / "测试居民甲_虚构地址.md"
        write(target_matter, matter_text("MATTER-904", "虚构新事项"))
        write(target_resident, resident_text("测试居民甲"))
        resident_business_before = target_resident.read_bytes()
        proc = run_cli(
            script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民甲_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        matter_before = target_matter.read_bytes()
        resident_before = target_resident.read_bytes()
        before_hashes = (digest(target_matter), digest(target_resident))

        # C：dry-run 必须零写入。
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-904_虚构新事项.md",
            "--resident-file",
            "04_居民档案/测试居民甲_虚构地址.md",
            "--dry-run",
        )
        assert proc.returncode == 0, proc.stderr
        dry_run_output = proc.stdout.rstrip()
        assert (digest(target_matter), digest(target_resident)) == before_hashes
        assert not list(root.rglob("*.wbp1-*"))
        assert not (root / ".workbench-phase1.lock").exists()
        results.append("C｜PASS｜dry-run 前后两个文件 SHA-256 均未变化，且未产生临时文件")

        # D：正式双向关联，两边必须同时成立，原业务内容保持原始字节前缀。
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-904_虚构新事项.md",
            "--resident-file",
            "04_居民档案/测试居民甲_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        matter_after = target_matter.read_text(encoding="utf-8")
        resident_after = target_resident.read_text(encoding="utf-8")
        assert "- RESIDENT-904｜04_居民档案/测试居民甲_虚构地址.md" in matter_after
        assert "居民ID：RESIDENT-904" in resident_after
        assert "- MATTER-904" in resident_after
        assert target_matter.read_bytes().startswith(matter_before)
        assert target_resident.read_bytes().startswith(resident_business_before)
        results.append("D｜PASS｜RESIDENT-904 ↔ MATTER-904 两侧同时写入，原业务内容字节前缀不变")

        # E：重复运行不能重写或重复追加。
        linked_hashes = (digest(target_matter), digest(target_resident))
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-904_虚构新事项.md",
            "--resident-file",
            "04_居民档案/测试居民甲_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        assert (digest(target_matter), digest(target_resident)) == linked_hashes
        assert target_matter.read_text(encoding="utf-8").count("RESIDENT-904｜") == 1
        assert target_resident.read_text(encoding="utf-8").count("- MATTER-904") == 1
        assert not (root / ".workbench-phase1.lock").exists()
        results.append("E｜PASS｜重复执行后两文件 SHA-256 不变，关联条目各只有一条")

        # F：在第二个正式替换前注入失败，核验第一侧已恢复为原始完整字节。
        fail_matter = matter_dir / "MATTER-905_虚构失败事项.md"
        fail_resident = resident_dir / "测试居民乙_虚构地址.md"
        write(fail_matter, matter_text("MATTER-905", "虚构失败事项"))
        write(fail_resident, resident_text("测试居民乙"))
        proc = run_cli(
            script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民乙_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        failure_before = (fail_matter.read_bytes(), fail_resident.read_bytes())
        spec = importlib.util.spec_from_file_location("workbench_phase1_under_test", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plan = module.prepare_link(
            root,
            "01_事项台账/MATTER-905_虚构失败事项.md",
            "04_居民档案/测试居民乙_虚构地址.md",
        )

        def fail_second_replace(_stage: Path, _target: Path, ordinal: int) -> None:
            if ordinal == 2:
                raise OSError("Phase 1 虚构第二侧写入失败")

        try:
            module.commit_link(plan, before_replace=fail_second_replace)
        except module.Phase1Error as exc:
            assert "已恢复所有已替换文件" in str(exc)
        else:
            raise AssertionError("虚构第二侧失败没有触发安全中止")
        assert fail_matter.read_bytes() == failure_before[0]
        assert fail_resident.read_bytes() == failure_before[1]
        assert not list(root.rglob("*.wbp1-*"))
        results.append("F｜PASS｜第二侧提交前故障后，第一侧恢复；两文件与故障前原始字节完全一致")

        # G：完全没有 MATTER 目录时，独立居民仍可 dry-run、分配唯一 ID 并正式初始化。
        independent_root = Path(temp_name) / "虚构独立居民工作台"
        independent_scripts = independent_root / "scripts"
        independent_scripts.mkdir(parents=True)
        independent_script = independent_scripts / "workbench_phase1.py"
        shutil.copy2(SCRIPT_SOURCE, independent_script)
        independent_residents = independent_root / "04_居民档案"
        write(independent_residents / "README.md", "# 虚构独立居民目录\n")
        write(
            independent_residents / "虚构既有居民710.md",
            resident_text("虚构既有居民710", "RESIDENT-710"),
        )
        write_registry(
            independent_root,
            {"04_居民档案/虚构既有居民710.md": "RESIDENT-710"},
        )
        independent_resident = independent_residents / "测试居民丙_虚构地址.md"
        write(independent_resident, resident_text("测试居民丙（纯虚构测试）"))
        independent_before = independent_resident.read_bytes()
        independent_hash = digest(independent_resident)
        independent_registry_hash = digest(
            independent_root / "scripts" / "_state" / "resident_registry.json"
        )
        assert not (independent_root / "01_事项台账").exists()

        proc = run_cli(
            independent_script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民丙_虚构地址.md",
            "--dry-run",
        )
        assert proc.returncode == 0, proc.stderr
        init_dry_run_output = proc.stdout.rstrip()
        assert "分配 RESIDENT-711，初始化空的“关联事项”" in proc.stdout
        assert digest(independent_resident) == independent_hash
        assert digest(
            independent_root / "scripts" / "_state" / "resident_registry.json"
        ) == independent_registry_hash
        assert not (independent_root / ".workbench-phase1.lock").exists()

        proc = run_cli(
            independent_script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民丙_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        initialized_text = independent_resident.read_text(encoding="utf-8")
        assert "居民ID：RESIDENT-711" in initialized_text
        assert "关联事项：" in initialized_text
        assert "- MATTER-" not in initialized_text
        assert independent_resident.read_bytes().startswith(independent_before)
        assert not (independent_root / "01_事项台账").exists()
        assert not (independent_root / ".workbench-phase1.lock").exists()
        assert not list(independent_root.rglob("*.wbp1-*"))
        results.append("G｜PASS｜无 MATTER 目录时分配 RESIDENT-711，仅初始化空关联事项，原居民正文前缀不变")

        # H：重复初始化幂等；随后现有 link 可给同一居民追加 MATTER，ID 不变化。
        initialized_hash = digest(independent_resident)
        proc = run_cli(
            independent_script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民丙_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        assert "未改写文件：RESIDENT-711" in proc.stdout
        assert digest(independent_resident) == initialized_hash
        proc = run_cli(independent_script, "next-resident-id")
        assert proc.returncode == 0 and proc.stdout.strip() == "RESIDENT-712"

        independent_matters = independent_root / "01_事项台账"
        write(independent_matters / "README.md", "# 后续虚构事项目录\n")
        later_matter = independent_matters / "MATTER-711_后续虚构事项.md"
        write(later_matter, matter_text("MATTER-711", "后续虚构事项"))
        proc = run_cli(
            independent_script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-711_后续虚构事项.md",
            "--resident-file",
            "04_居民档案/测试居民丙_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        linked_resident_text = independent_resident.read_text(encoding="utf-8")
        linked_matter_text = later_matter.read_text(encoding="utf-8")
        assert linked_resident_text.count("居民ID：RESIDENT-711") == 1
        assert linked_resident_text.count("- MATTER-711") == 1
        assert "- RESIDENT-711｜04_居民档案/测试居民丙_虚构地址.md" in linked_matter_text
        linked_hash = digest(independent_resident)
        proc = run_cli(
            independent_script,
            "init-resident",
            "--resident-file",
            "04_居民档案/测试居民丙_虚构地址.md",
        )
        assert proc.returncode == 0, proc.stderr
        assert digest(independent_resident) == linked_hash
        assert independent_resident.read_text(encoding="utf-8").count("- MATTER-711") == 1
        results.append("H｜PASS｜重复初始化不改写；后续 link 沿用 RESIDENT-711 并正常追加 MATTER-711")

        # I：独立初始化提交前注入故障，原居民文件保持完整字节且无残留。
        module2_spec = importlib.util.spec_from_file_location(
            "workbench_phase1_init_under_test", independent_script
        )
        assert module2_spec is not None and module2_spec.loader is not None
        module2 = importlib.util.module_from_spec(module2_spec)
        sys.modules[module2_spec.name] = module2
        module2_spec.loader.exec_module(module2)
        init_fail_resident = independent_residents / "虚构初始化失败居民.md"
        write(init_fail_resident, resident_text("虚构初始化失败居民"))
        init_fail_before = init_fail_resident.read_bytes()
        init_fail_registry_before = (
            independent_root / "scripts" / "_state" / "resident_registry.json"
        ).read_bytes()
        init_plan = module2.prepare_init_resident(
            independent_root,
            "04_居民档案/虚构初始化失败居民.md",
        )

        def fail_init_replace(_stage: Path, _target: Path) -> None:
            raise OSError("REAL-04 虚构单文件替换前故障")

        try:
            module2.commit_init_resident(init_plan, before_replace=fail_init_replace)
        except module2.Phase1Error as exc:
            assert "原文件未改变" in str(exc)
        else:
            raise AssertionError("虚构居民初始化故障没有触发安全中止")
        assert init_fail_resident.read_bytes() == init_fail_before
        assert (independent_root / "scripts" / "_state" / "resident_registry.json").read_bytes() == init_fail_registry_before
        assert not list(independent_root.rglob("*.wbp1-*"))
        results.append("I｜PASS｜独立初始化提交前故障被明确中止，registry 与居民文件保持原始完整字节且无残留")

        # J：清理 unlink 失败不能误报已验证成功的提交，也不能覆盖真正的提交异常。
        cleanup_success_resident = independent_residents / "虚构清理失败但提交成功居民.md"
        write(cleanup_success_resident, resident_text("虚构清理失败但提交成功居民"))
        cleanup_success_plan = module2.prepare_init_resident(
            independent_root,
            "04_居民档案/虚构清理失败但提交成功居民.md",
        )
        real_unlink = module2.Path.unlink

        def fail_rollback_unlink(path: Path, *args, **kwargs) -> None:
            if path.name.endswith(".rollback"):
                raise OSError("REAL-04 虚构清理 unlink 失败")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(module2.Path, "unlink", new=fail_rollback_unlink):
            module2.commit_init_resident(cleanup_success_plan)
        assert cleanup_success_resident.read_bytes() == cleanup_success_plan.resident_after
        assert (
            independent_root / "scripts" / "_state" / "resident_registry.json"
        ).read_bytes() == cleanup_success_plan.registry_after
        cleanup_success_text = cleanup_success_resident.read_text(encoding="utf-8")
        assert f"居民ID：{cleanup_success_plan.resident_id}" in cleanup_success_text
        cleanup_residuals = list(independent_root.rglob("*.rollback"))
        assert len(cleanup_residuals) == 2
        for residual in cleanup_residuals:
            residual.unlink()

        genuine_fail_resident = independent_residents / "虚构真实提交异常居民.md"
        write(genuine_fail_resident, resident_text("虚构真实提交异常居民"))
        genuine_fail_before = genuine_fail_resident.read_bytes()
        genuine_registry_before = (
            independent_root / "scripts" / "_state" / "resident_registry.json"
        ).read_bytes()
        genuine_fail_plan = module2.prepare_init_resident(
            independent_root,
            "04_居民档案/虚构真实提交异常居民.md",
        )

        def genuine_submit_failure(_stage: Path, _target: Path) -> None:
            raise OSError("REAL-04 虚构真正提交异常")

        def fail_all_temp_cleanup(_path: Path, *args, **kwargs) -> None:
            raise OSError("REAL-04 虚构伴随清理异常")

        with mock.patch.object(module2.Path, "unlink", new=fail_all_temp_cleanup):
            try:
                module2.commit_init_resident(
                    genuine_fail_plan,
                    before_replace=genuine_submit_failure,
                )
            except module2.Phase1Error as exc:
                assert "REAL-04 虚构真正提交异常" in str(exc)
                assert "REAL-04 虚构伴随清理异常" not in str(exc)
            else:
                raise AssertionError("真正提交异常被错误视为成功")
        assert genuine_fail_resident.read_bytes() == genuine_fail_before
        assert (
            independent_root / "scripts" / "_state" / "resident_registry.json"
        ).read_bytes() == genuine_registry_before
        for residual in independent_root.rglob("*.wbp1-*"):
            residual.unlink()
        results.append("J｜PASS｜清理 unlink 失败不误报已验证成功提交，也不覆盖真正的提交异常")

        # K：恢复 replace 已消耗 backup 后若字节验证失败，不得声称 backup 仍被保留。
        rollback_verify_resident = independent_residents / "虚构恢复验证失败居民.md"
        write(rollback_verify_resident, resident_text("虚构恢复验证失败居民"))
        rollback_verify_plan = module2.prepare_init_resident(
            independent_root,
            "04_居民档案/虚构恢复验证失败居民.md",
        )
        real_replace = module2.os.replace
        replace_calls = 0

        def replace_then_corrupt(source: Path, target: Path) -> None:
            nonlocal replace_calls
            replace_calls += 1
            real_replace(source, target)
            target_path = Path(target)
            if replace_calls == 1:
                target_path.write_bytes(rollback_verify_plan.resident_after + b"corrupt-new")
            elif replace_calls == 2:
                target_path.write_bytes(b"corrupt-restored-bytes")

        with mock.patch.object(module2.os, "replace", new=replace_then_corrupt):
            try:
                module2.commit_init_resident(rollback_verify_plan)
            except module2.Phase1Error as exc:
                failure_text = str(exc)
                assert "恢复副本已不存在，正式文件状态需人工核查" in failure_text
                assert "恢复副本已保留" not in failure_text
            else:
                raise AssertionError("恢复后字节验证失败没有触发安全中止")
        assert replace_calls == 2
        assert not list(independent_root.rglob("*.rollback"))
        results.append("K｜PASS｜恢复 replace 已消耗 backup 后验证失败，报告明确 backup 已不存在")

    print("=== DRY-RUN ACTUAL OUTPUT ===")
    print(dry_run_output)
    print("=== INIT-RESIDENT DRY-RUN ACTUAL OUTPUT ===")
    print(init_dry_run_output)
    print("=== PHASE 1 SCENARIOS ===")
    for result in results:
        print(result)
    print("测试临时目录已由 TemporaryDirectory 清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
