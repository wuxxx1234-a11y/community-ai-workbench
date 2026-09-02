#!/usr/bin/env python3
"""在系统临时目录用纯虚构文件验收最小 RESIDENT-ID registry。"""

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


SCRIPT_SOURCE = Path(__file__).resolve().with_name("workbench_phase1.py")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resident_text(name: str, resident_id: str | None = None, malformed: bool = False) -> str:
    base = f"""# {name}

## 基本信息

| 项目 | 内容 | 来源 |
|------|------|------|
| 姓名 | {name} | registry 虚构测试 |
| 地址 | 虚构地址 | registry 虚构测试 |
"""
    if malformed:
        return base + "\n## 工作台关联（系统维护）\n\n居民ID：RESIDENT-异常\n"
    if resident_id is None:
        return base
    return base + f"""
## 工作台关联（系统维护）

居民ID：{resident_id}

关联事项：
"""


def matter_text(identifier: str) -> str:
    return f"""# {identifier}｜registry 虚构事项

## 基本信息

| 项目 | 内容 |
|------|------|
| MATTER-ID | {identifier} |
| 来源 | [工作人员] registry 虚构测试 |
| 状态 | 虚构状态 |

## 事项正文

完全虚构，不对应真实业务。
"""


def make_root(parent: Path, name: str) -> tuple[Path, Path]:
    root = parent / name
    script = root / "scripts" / "workbench_phase1.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT_SOURCE, script)
    write(root / "04_居民档案" / "README.md", "# 虚构居民目录\n")
    return root, script


def registry_path(root: Path) -> Path:
    return root / "scripts" / "_state" / "resident_registry.json"


def write_registry(root: Path, entries: dict[str, str]) -> Path:
    path = registry_path(root)
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


def load_module(script: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def assert_clean(root: Path) -> None:
    assert not (root / ".workbench-phase1.lock").exists()
    assert not list(root.rglob("*.wbp1-*"))


def main() -> int:
    r0_results: list[str] = []
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="community-ai-resident-registry-") as temp_name:
        temp = Path(temp_name)

        root, script = make_root(temp, "R0_未bootstrap工作区")
        state_dir = root / "scripts" / "_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        registry = registry_path(root)
        assert state_dir.is_dir() and not registry.exists()
        plain = root / "04_居民档案" / "虚构R0居民.md"
        matter = root / "01_事项台账" / "MATTER-001_虚构R0事项.md"
        write(plain, resident_text("虚构R0居民"))
        write(matter, matter_text("MATTER-001"))

        plain_before = plain.read_bytes()
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构R0居民.md")
        assert proc.returncode == 2 and "缺少 RESIDENT-ID 权威 registry" in proc.stderr
        assert plain.read_bytes() == plain_before and not registry.exists()
        assert_clean(root)
        r0_results.append("R0-1｜PASS｜普通 init-resident 停止，未创建 registry，居民文件未修改")

        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 2 and "缺少 RESIDENT-ID 权威 registry" in proc.stderr
        assert not registry.exists()
        assert_clean(root)
        r0_results.append("R0-2｜PASS｜next-resident-id 停止，未把缺失解释为 {}，未退回居民扫描")

        business_before = (matter.read_bytes(), plain.read_bytes())
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-001_虚构R0事项.md",
            "--resident-file",
            "04_居民档案/虚构R0居民.md",
        )
        assert proc.returncode == 2 and "缺少 RESIDENT-ID 权威 registry" in proc.stderr
        assert (matter.read_bytes(), plain.read_bytes()) == business_before
        assert not registry.exists()
        assert_clean(root)
        r0_results.append("R0-3｜PASS｜正式 link 写操作停止，未创建 registry，业务文件未修改")

        proc = run_cli(script, "bootstrap-resident-registry", "--list")
        assert proc.returncode == 0, proc.stderr
        assert "未发现历史迁移候选" in proc.stdout
        assert not registry.exists()
        assert_clean(root)
        r0_results.append("R0-4｜PASS｜bootstrap --list 只读完成，registry 仍不存在")

        failed_root, failed_script = make_root(temp, "R0_失败bootstrap工作区")
        (failed_root / "scripts" / "_state").mkdir(parents=True, exist_ok=True)
        write(
            failed_root / "04_居民档案" / "虚构异常迁移对象.md",
            resident_text("虚构异常迁移对象", malformed=True),
        )
        failed_registry = registry_path(failed_root)
        proc = run_cli(failed_script, "bootstrap-resident-registry", "--list")
        assert proc.returncode == 0 and "[异常]" in proc.stdout
        assert not failed_registry.exists()
        proc = run_cli(failed_script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 2 and "未闭合" in proc.stderr
        assert not failed_registry.exists()
        assert_clean(failed_root)
        assert not registry.exists()
        r0_results.append("R0-5｜PASS｜只读及异常失败路径均未产生 registry")

        assert not registry.exists()
        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 0, proc.stderr
        assert "bootstrap 完成" in proc.stdout
        assert registry.is_file()
        r0_results.append("R0-6｜PASS｜无历史候选时仅在明确 --confirm-all 后首次创建 {}")

        registry_raw = registry.read_bytes()
        registry_json = json.loads(registry_raw.decode("utf-8"))
        assert registry_raw == b"{}\n" and registry_json == {}
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 0 and proc.stdout.strip() == "RESIDENT-001"
        r0_results.append("R0-7｜PASS｜bootstrap 创建结果经重新读取和 JSON 校验为 {}，随后被权威读取")

        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 2 and "已存在" in proc.stderr
        assert registry.read_bytes() == registry_raw
        assert_clean(root)
        r0_results.append("R0-8｜PASS｜bootstrap 成功后再次正式创建被拒绝，既有 registry 未改写")

        root, script = make_root(temp, "A_空bootstrap")
        plain = root / "04_居民档案" / "虚构普通居民.md"
        write(plain, resident_text("虚构普通居民"))
        plain_before = plain.read_bytes()
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构普通居民.md")
        assert proc.returncode == 2 and "缺少 RESIDENT-ID 权威 registry" in proc.stderr
        assert plain.read_bytes() == plain_before and not registry_path(root).exists()
        proc = run_cli(script, "bootstrap-resident-registry", "--list")
        assert proc.returncode == 0, proc.stderr
        assert "未发现历史迁移候选" in proc.stdout
        assert not registry_path(root).exists()
        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 0, proc.stderr
        assert registry_path(root).read_text(encoding="utf-8") == "{}\n"
        assert json.loads(registry_path(root).read_text(encoding="utf-8")) == {}
        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 2 and "已存在" in proc.stderr
        results.append("R1｜PASS｜registry 缺失时普通 init 停止；空候选经明确 confirm-all 创建 {}，重复创建被拒绝")

        root, script = make_root(temp, "B_候选未闭合")
        write(root / "04_居民档案" / "虚构合法候选.md", resident_text("虚构合法候选", "RESIDENT-101"))
        write(root / "04_居民档案" / "虚构异常候选.md", resident_text("虚构异常候选", malformed=True))
        proc = run_cli(script, "bootstrap-resident-registry", "--list")
        assert proc.returncode == 0, proc.stderr
        assert "[待确认]" in proc.stdout and "[异常]" in proc.stdout
        assert not registry_path(root).exists()
        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 2 and "未闭合" in proc.stderr
        assert not registry_path(root).exists()
        results.append("R2｜PASS｜候选未全部确认/存在异常时只读列出，正式 bootstrap 整体停止且不创建部分 registry")

        root, script = make_root(temp, "C_完整bootstrap")
        first = root / "04_居民档案" / "虚构候选甲.md"
        second = root / "04_居民档案" / "虚构候选乙.md"
        write(first, resident_text("虚构候选甲", "RESIDENT-201"))
        write(second, resident_text("虚构候选乙", "RESIDENT-203"))
        proc = run_cli(script, "bootstrap-resident-registry", "--confirm-all")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(registry_path(root).read_text(encoding="utf-8")) == {
            "04_居民档案/虚构候选甲.md": "RESIDENT-201",
            "04_居民档案/虚构候选乙.md": "RESIDENT-203",
        }
        results.append("R3｜PASS｜全部合法候选经 confirm-all 后一次性完整写入首次 registry")

        root, script = make_root(temp, "D_重复key")
        duplicate_key = "04_居民档案/虚构重复key.md"
        write(
            registry_path(root),
            '{"' + duplicate_key + '":"RESIDENT-301","' + duplicate_key + '":"RESIDENT-302"}\n',
        )
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 2 and "重复 JSON key" in proc.stderr
        results.append("R4｜PASS｜duplicate JSON key 在解析阶段被拒绝")

        root, script = make_root(temp, "D2_JSON损坏")
        write(registry_path(root), "{损坏的JSON\n")
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 2 and "JSON 损坏" in proc.stderr
        results.append("R4b｜PASS｜损坏的 registry JSON 被拒绝且不从居民文件重建")

        root, script = make_root(temp, "E_重复ID")
        write_registry(
            root,
            {
                "04_居民档案/虚构居民甲.md": "RESIDENT-401",
                "04_居民档案/虚构居民乙.md": "RESIDENT-401",
            },
        )
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 2 and "重复 RESIDENT-ID" in proc.stderr
        results.append("R5｜PASS｜registry 中重复 RESIDENT-ID 被拒绝")

        root, script = make_root(temp, "F_registry权威编号")
        write_registry(
            root,
            {
                "04_居民档案/虚构已登记.md": "RESIDENT-005",
                "04_居民档案/虚构已登记但文件缺失.md": "RESIDENT-006",
            },
        )
        write(root / "04_居民档案" / "虚构已登记.md", resident_text("虚构已登记", "RESIDENT-005"))
        write(root / "04_居民档案" / "虚构外部高号.md", resident_text("虚构外部高号", "RESIDENT-999"))
        proc = run_cli(script, "next-resident-id")
        assert proc.returncode == 0 and proc.stdout.strip() == "RESIDENT-007"
        results.append("R6｜PASS｜next-resident-id 只取 registry；缺失文件的 RESIDENT-006 不回收，预写 RESIDENT-999 不影响编号")

        root, script = make_root(temp, "G_L2")
        registry = write_registry(root, {})
        target = root / "04_居民档案" / "虚构L2居民.md"
        write(target, resident_text("虚构L2居民", "RESIDENT-999"))
        before = (registry.read_bytes(), target.read_bytes())
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构L2居民.md")
        assert proc.returncode == 2 and "未经登记的系统状态" in proc.stderr
        assert (registry.read_bytes(), target.read_bytes()) == before
        assert_clean(root)
        results.append("L2｜PASS｜未登记居民预写合法 RESIDENT-999 后 init-resident 明确拒绝，未沿用或改写")

        root, script = make_root(temp, "H_幂等与异常")
        consistent = root / "04_居民档案" / "虚构一致居民.md"
        missing = root / "04_居民档案" / "虚构缺区居民.md"
        mismatch = root / "04_居民档案" / "虚构错号居民.md"
        malformed = root / "04_居民档案" / "虚构异常系统区居民.md"
        write(consistent, resident_text("虚构一致居民", "RESIDENT-501"))
        write(missing, resident_text("虚构缺区居民"))
        write(mismatch, resident_text("虚构错号居民", "RESIDENT-599"))
        write(malformed, resident_text("虚构异常系统区居民", malformed=True))
        registry = write_registry(
            root,
            {
                "04_居民档案/虚构一致居民.md": "RESIDENT-501",
                "04_居民档案/虚构缺区居民.md": "RESIDENT-502",
                "04_居民档案/虚构错号居民.md": "RESIDENT-503",
                "04_居民档案/虚构异常系统区居民.md": "RESIDENT-504",
            },
        )
        consistent_before = (digest(registry), digest(consistent))
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构一致居民.md")
        assert proc.returncode == 0, proc.stderr
        assert (digest(registry), digest(consistent)) == consistent_before
        missing_before = missing.read_bytes()
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构缺区居民.md")
        assert proc.returncode == 2 and "缺少系统维护区" in proc.stderr
        assert missing.read_bytes() == missing_before
        mismatch_before = mismatch.read_bytes()
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构错号居民.md")
        assert proc.returncode == 2 and "与 registry 不一致" in proc.stderr
        assert mismatch.read_bytes() == mismatch_before
        malformed_before = malformed.read_bytes()
        proc = run_cli(script, "init-resident", "--resident-file", "04_居民档案/虚构异常系统区居民.md")
        assert proc.returncode == 2 and "系统维护区异常" in proc.stderr
        assert malformed.read_bytes() == malformed_before
        assert_clean(root)
        results.append("R7｜PASS｜已登记且一致时幂等；系统区缺失、异常或 ID 不一致时停止且不自动修复")

        root, script = make_root(temp, "I_link拒绝未登记")
        resident = root / "04_居民档案" / "虚构未登记居民.md"
        matter = root / "01_事项台账" / "MATTER-601_虚构事项.md"
        write(resident, resident_text("虚构未登记居民", "RESIDENT-601"))
        write(matter, matter_text("MATTER-601"))
        write_registry(root, {})
        before = (resident.read_bytes(), matter.read_bytes())
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-601_虚构事项.md",
            "--resident-file",
            "04_居民档案/虚构未登记居民.md",
        )
        assert proc.returncode == 2 and "尚未登记" in proc.stderr
        assert (resident.read_bytes(), matter.read_bytes()) == before
        write_registry(root, {"04_居民档案/虚构未登记居民.md": "RESIDENT-602"})
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-601_虚构事项.md",
            "--resident-file",
            "04_居民档案/虚构未登记居民.md",
        )
        assert proc.returncode == 2 and "与 registry 不一致" in proc.stderr
        assert (resident.read_bytes(), matter.read_bytes()) == before
        assert_clean(root)
        results.append("R8｜PASS｜link 拒绝 registry 未登记或文件 ID 不一致的居民且不修改两侧文件")

        root, script = make_root(temp, "J_两文件事务")
        registry = write_registry(root, {"04_居民档案/虚构既有居民.md": "RESIDENT-701"})
        write(root / "04_居民档案" / "虚构既有居民.md", resident_text("虚构既有居民", "RESIDENT-701"))
        first_target = root / "04_居民档案" / "虚构registry侧失败.md"
        second_target = root / "04_居民档案" / "虚构resident侧失败.md"
        write(first_target, resident_text("虚构registry侧失败"))
        write(second_target, resident_text("虚构resident侧失败"))
        module = load_module(script, "workbench_phase1_registry_transaction_test")

        before = (registry.read_bytes(), first_target.read_bytes())
        with module.workspace_write_lock(root):
            plan = module.prepare_init_resident(root, "04_居民档案/虚构registry侧失败.md")

            def fail_registry(_stage: Path, target_path: Path) -> None:
                if Path(target_path) == plan.registry.path:
                    raise OSError("虚构 registry 侧提交失败")

            try:
                module.commit_init_resident(plan, before_replace=fail_registry)
            except module.Phase1Error as exc:
                assert "原文件未改变" in str(exc)
            else:
                raise AssertionError("registry 侧故障没有触发失败")
        assert (registry.read_bytes(), first_target.read_bytes()) == before
        assert_clean(root)

        before = (registry.read_bytes(), second_target.read_bytes())
        with module.workspace_write_lock(root):
            plan = module.prepare_init_resident(root, "04_居民档案/虚构resident侧失败.md")

            def fail_resident(_stage: Path, target_path: Path) -> None:
                if Path(target_path) == plan.resident.path:
                    raise OSError("虚构 resident 侧提交失败")

            try:
                module.commit_init_resident(plan, before_replace=fail_resident)
            except module.Phase1Error as exc:
                assert "已恢复所有已替换文件" in str(exc)
            else:
                raise AssertionError("resident 侧故障没有触发失败")
        assert (registry.read_bytes(), second_target.read_bytes()) == before
        assert_clean(root)
        results.append("R9｜PASS｜registry 或居民文件任一侧提交失败均不留下半登记，原始字节全部恢复")

        root, script = make_root(temp, "K_既有link")
        resident = root / "04_居民档案" / "虚构已登记居民.md"
        matter = root / "01_事项台账" / "MATTER-801_虚构事项.md"
        write(resident, resident_text("虚构已登记居民", "RESIDENT-801"))
        write(matter, matter_text("MATTER-801"))
        write_registry(root, {"04_居民档案/虚构已登记居民.md": "RESIDENT-801"})
        matter_prefix = matter.read_bytes()
        resident_prefix = resident.read_bytes()
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-801_虚构事项.md",
            "--resident-file",
            "04_居民档案/虚构已登记居民.md",
            "--dry-run",
        )
        assert proc.returncode == 0, proc.stderr
        assert matter.read_bytes() == matter_prefix and resident.read_bytes() == resident_prefix
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-801_虚构事项.md",
            "--resident-file",
            "04_居民档案/虚构已登记居民.md",
        )
        assert proc.returncode == 0, proc.stderr
        linked_hashes = (digest(matter), digest(resident))
        assert matter.read_bytes().startswith(matter_prefix)
        assert resident.read_bytes().startswith(resident_prefix)
        proc = run_cli(
            script,
            "link",
            "--matter-file",
            "01_事项台账/MATTER-801_虚构事项.md",
            "--resident-file",
            "04_居民档案/虚构已登记居民.md",
        )
        assert proc.returncode == 0, proc.stderr
        assert (digest(matter), digest(resident)) == linked_hashes
        assert_clean(root)
        results.append("R10｜PASS｜已登记居民的既有 link dry-run、双向写入和重复幂等行为保持")

    print("=== RESIDENT REGISTRY R0 TESTS ===")
    for result in r0_results:
        print(result)
    print("=== RESIDENT REGISTRY TESTS ===")
    for result in results:
        print(result)
    print("测试临时目录已由 TemporaryDirectory 清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
