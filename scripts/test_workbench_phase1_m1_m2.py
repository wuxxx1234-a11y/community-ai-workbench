#!/usr/bin/env python3
"""用系统临时目录和纯虚构数据验证 init-resident 双文件事务的 M1/M2。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock


SCRIPT_SOURCE = Path(__file__).resolve().with_name("workbench_phase1.py")
RESIDENT_RELATIVE = "04_居民档案/虚构双文件事务居民.md"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resident_text(name: str) -> str:
    return f"""# {name}

## 基本信息

| 项目 | 内容 | 来源 |
|------|------|------|
| 姓名 | {name} | M1/M2 纯虚构测试 |
| 地址 | 虚构地址 | M1/M2 纯虚构测试 |
"""


def matter_text() -> str:
    return """# MATTER-901｜M1/M2 无关虚构事项

## 基本信息

| 项目 | 内容 |
|------|------|
| MATTER-ID | MATTER-901 |
| 来源 | [工作人员] M1/M2 纯虚构测试 |
| 状态 | 虚构状态 |

## 事项正文

此文件只用于证明其他业务文件没有变化。
"""


def make_root(parent: Path, name: str) -> tuple[Path, Path, Path, Path]:
    root = parent / name
    script = root / "scripts" / "workbench_phase1.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT_SOURCE, script)
    registry = root / "scripts" / "_state" / "resident_registry.json"
    write(registry, "{}\n")
    write(root / "04_居民档案" / "README.md", "# M1/M2 虚构居民目录\n")
    resident = root / RESIDENT_RELATIVE
    write(resident, resident_text("虚构双文件事务居民"))
    write(root / "04_居民档案" / "虚构无关居民.md", resident_text("虚构无关居民"))
    write(root / "01_事项台账" / "MATTER-901_无关虚构事项.md", matter_text())
    return root, script, registry, resident


def load_module(script: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def transaction_residuals(root: Path) -> tuple[list[Path], list[Path], bool]:
    staged = [
        path
        for path in root.rglob("*")
        if path.is_file() and ".wbp1-" in path.name and path.name.endswith(".new")
    ]
    backups = [
        path
        for path in root.rglob("*")
        if path.is_file() and ".wbp1-" in path.name and path.name.endswith(".rollback")
    ]
    return staged, backups, (root / ".workbench-phase1.lock").exists()


def preparation_recorder(module, records: list[tuple[Path, str]]):
    real_write_temp = module._write_temp_bytes

    def record(target: Path, raw: bytes, mode: int, suffix: str) -> Path:
        result = real_write_temp(target, raw, mode, suffix)
        records.append((Path(target), suffix))
        return result

    return record


def expected_preparation(plan) -> list[tuple[Path, str]]:
    return [
        (plan.registry.path, ".new"),
        (plan.resident.path, ".new"),
        (plan.registry.path, ".rollback"),
        (plan.resident.path, ".rollback"),
    ]


def parsed_state(module, root: Path, registry: Path, resident: Path):
    registry_state = module._parse_registry_bytes(registry, registry.read_bytes())
    resident_document = module._read_document(root, resident)
    resident_state = module._parse_resident_region(resident_document.text)
    return registry_state, resident_state


def run_m1(parent: Path) -> list[str]:
    root, script, registry, resident = make_root(parent, "M1_两侧替换后最终验证失败")
    module = load_module(script, "workbench_phase1_m1_test")
    plan = module.prepare_init_resident(root, RESIDENT_RELATIVE)
    before_registry = registry.read_bytes()
    before_resident = resident.read_bytes()
    before_hashes = (digest(registry), digest(resident))
    before_parsed = parsed_state(module, root, registry, resident)
    before_tree = tree_snapshot(root)
    prepared: list[tuple[Path, str]] = []
    replace_events: list[tuple[str, Path, Path]] = []
    injection: dict[str, object] = {}
    real_replace = module.os.replace
    real_load_registry = module.load_resident_registry
    load_calls = 0

    def record_replace(source: Path, target: Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        real_replace(source_path, target_path)
        kind = "official" if source_path.name.endswith(".new") else "rollback"
        replace_events.append((kind, source_path, target_path))

    def fail_at_final_registry_reload(root_path: Path):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 2:
            official = [event for event in replace_events if event[0] == "official"]
            injection.update(
                {
                    "prepared": prepared == expected_preparation(plan),
                    "official_replace_count": len(official),
                    "official_targets": [event[2] for event in official],
                    "registry_is_new": registry.read_bytes() == plan.registry_after,
                    "resident_is_new": resident.read_bytes() == plan.resident_after,
                }
            )
            raise module.Phase1Error("M1 故意注入：两侧替换完成后的最终 registry 重读验证失败")
        return real_load_registry(root_path)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(module, "_write_temp_bytes", new=preparation_recorder(module, prepared)):
        with mock.patch.object(module.os, "replace", new=record_replace):
            with mock.patch.object(module, "load_resident_registry", new=fail_at_final_registry_reload):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    return_code = module.main(
                        ["init-resident", "--resident-file", RESIDENT_RELATIVE]
                    )

    after_registry = registry.read_bytes()
    after_resident = resident.read_bytes()
    after_hashes = (digest(registry), digest(resident))
    after_parsed = parsed_state(module, root, registry, resident)
    after_tree = tree_snapshot(root)
    staged, backups, lock_exists = transaction_residuals(root)
    official = [event for event in replace_events if event[0] == "official"]
    rollbacks = [event for event in replace_events if event[0] == "rollback"]

    criteria = {
        "命令最终返回失败": return_code == 2,
        "四个 staged/backup 均在替换前准备完成": injection.get("prepared") is True,
        "两个正式 os.replace 均成功后才注入": (
            injection.get("official_replace_count") == 2
            and injection.get("official_targets") == [plan.registry.path, plan.resident.path]
            and len(official) == 2
        ),
        "故障瞬间 registry 已为新字节": injection.get("registry_is_new") is True,
        "故障瞬间 resident 已为新字节": injection.get("resident_is_new") is True,
        "registry 最终字节逐字节恢复": after_registry == before_registry,
        "resident 最终字节逐字节恢复": after_resident == before_resident,
        "registry SHA256 恢复": after_hashes[0] == before_hashes[0],
        "resident SHA256 恢复": after_hashes[1] == before_hashes[1],
        "双方重新解析状态恢复": after_parsed == before_parsed,
        "registry 无新居民映射": RESIDENT_RELATIVE not in after_parsed[0],
        "resident 无新系统状态": after_parsed[1] is None,
        "staged 残留为 0": not staged,
        "backup 残留为 0": not backups,
        "lock 残留为 0": not lock_exists,
        "其他文件无变化": after_tree == before_tree,
        "两个已替换文件均执行 rollback": len(rollbacks) == 2,
        "失败被明确报告为已恢复": "已恢复所有已替换文件" in stderr.getvalue(),
    }
    assert all(criteria.values()), {
        "criteria": criteria,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "replace_events": replace_events,
    }
    return [f"M1｜{name}｜PASS" for name in criteria]


def run_m2(parent: Path) -> list[str]:
    root, script, registry, resident = make_root(parent, "M2_registry替换后resident替换失败")
    module = load_module(script, "workbench_phase1_m2_test")
    plan = module.prepare_init_resident(root, RESIDENT_RELATIVE)
    before_registry = registry.read_bytes()
    before_resident = resident.read_bytes()
    before_hashes = (digest(registry), digest(resident))
    before_parsed = parsed_state(module, root, registry, resident)
    before_tree = tree_snapshot(root)
    prepared: list[tuple[Path, str]] = []
    replace_events: list[tuple[str, Path, Path]] = []
    injection: dict[str, object] = {}
    real_replace = module.os.replace

    def fail_resident_official_replace(source: Path, target: Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        is_official = source_path.name.endswith(".new")
        if is_official and target_path == plan.resident.path:
            official = [event for event in replace_events if event[0] == "official"]
            injection.update(
                {
                    "prepared": prepared == expected_preparation(plan),
                    "registry_replace_succeeded": (
                        len(official) == 1 and official[0][2] == plan.registry.path
                    ),
                    "registry_is_new": registry.read_bytes() == plan.registry_after,
                    "resident_is_original": resident.read_bytes() == before_resident,
                    "resident_replace_completed": False,
                }
            )
            raise OSError("M2 故意注入：registry 替换成功后 resident 正式 os.replace 失败")
        real_replace(source_path, target_path)
        kind = "official" if is_official else "rollback"
        replace_events.append((kind, source_path, target_path))

    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(module, "_write_temp_bytes", new=preparation_recorder(module, prepared)):
        with mock.patch.object(module.os, "replace", new=fail_resident_official_replace):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = module.main(
                    ["init-resident", "--resident-file", RESIDENT_RELATIVE]
                )

    after_registry = registry.read_bytes()
    after_resident = resident.read_bytes()
    after_hashes = (digest(registry), digest(resident))
    after_parsed = parsed_state(module, root, registry, resident)
    after_tree = tree_snapshot(root)
    staged, backups, lock_exists = transaction_residuals(root)
    official = [event for event in replace_events if event[0] == "official"]
    rollbacks = [event for event in replace_events if event[0] == "rollback"]

    criteria = {
        "命令最终返回失败": return_code == 2,
        "四个 staged/backup 均在替换前准备完成": injection.get("prepared") is True,
        "registry 正式 os.replace 已成功": (
            injection.get("registry_replace_succeeded") is True
            and len(official) == 1
            and official[0][2] == plan.registry.path
        ),
        "故障瞬间 registry 已含新映射": injection.get("registry_is_new") is True,
        "故障瞬间 resident 仍为原始状态": injection.get("resident_is_original") is True,
        "resident 正式替换未完成": injection.get("resident_replace_completed") is False,
        "registry 最终字节逐字节恢复": after_registry == before_registry,
        "resident 最终字节保持原始": after_resident == before_resident,
        "registry SHA256 恢复": after_hashes[0] == before_hashes[0],
        "resident SHA256 保持": after_hashes[1] == before_hashes[1],
        "双方重新解析状态与执行前一致": after_parsed == before_parsed,
        "registry 无半登记映射": RESIDENT_RELATIVE not in after_parsed[0],
        "resident 无半初始化状态": after_parsed[1] is None,
        "staged 残留为 0": not staged,
        "backup 残留为 0": not backups,
        "lock 残留为 0": not lock_exists,
        "其他文件无变化": after_tree == before_tree,
        "registry 已执行一次 rollback": len(rollbacks) == 1 and rollbacks[0][2] == plan.registry.path,
        "失败被明确报告为已恢复": "已恢复所有已替换文件" in stderr.getvalue(),
    }
    assert all(criteria.values()), {
        "criteria": criteria,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "replace_events": replace_events,
    }
    return [f"M2｜{name}｜PASS" for name in criteria]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="community-ai-phase1-m1-m2-") as temp_name:
        parent = Path(temp_name)
        m1_results = run_m1(parent)
        m2_results = run_m2(parent)
    print("=== M1 RESULTS ===")
    for result in m1_results:
        print(result)
    print("=== M2 RESULTS ===")
    for result in m2_results:
        print(result)
    print("M1/M2 测试临时目录已由 TemporaryDirectory 清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
