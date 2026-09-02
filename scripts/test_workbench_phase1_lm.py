#!/usr/bin/env python3
"""REAL-04 L/M 定向验证；只在系统临时目录使用纯虚构文件。"""

from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resident_text(name: str, resident_id: str | None = None) -> str:
    text = f"""# {name}

## 基本信息

| 项目 | 内容 | 来源 |
|------|------|------|
| 姓名 | {name} | REAL-04 L/M 纯虚构测试 |
| 地址 | 虚构地址 | REAL-04 L/M 纯虚构测试 |
"""
    if resident_id is None:
        return text
    return text + f"""
## 工作台关联（系统维护）

居民ID：{resident_id}

关联事项：
"""


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
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_root(temp_name: str, source: Path) -> tuple[Path, Path]:
    root = Path(temp_name) / "虚构居民初始化工作台"
    script_dir = root / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "workbench_phase1.py"
    shutil.copy2(source, script)
    write(script_dir / "_state" / "resident_registry.json", "{}\n")
    write(root / "04_居民档案" / "README.md", "# 虚构居民目录\n")
    return root, script


def init_cli_options(module) -> list[str]:
    parser = module._build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    init_parser = subparsers.choices["init-resident"]
    return sorted(
        option
        for action in init_parser._actions
        for option in action.option_strings
        if option not in {"-h", "--help"}
    )


def run_l(source: Path) -> int:
    source_text = source.read_text(encoding="utf-8-sig")
    tree = ast.parse(source_text, filename=str(source))
    prepare = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prepare_init_resident"
    )
    prepare_parameters = [argument.arg for argument in prepare.args.args]
    static_markers = {
        "os.environ": "os.environ" in source_text,
        "os.getenv": "os.getenv" in source_text,
        "getenv_call": "getenv(" in source_text,
        "plan_file_option": "--plan-file" in source_text,
        "configparser": "configparser" in source_text,
    }

    with tempfile.TemporaryDirectory(prefix="community-ai-real04-l-") as temp_name:
        root, script = make_root(temp_name, source)
        module = load_module(script, "workbench_phase1_l_under_test")
        options = init_cli_options(module)
        assert prepare_parameters == ["root", "resident_relative"]
        assert options == ["--dry-run", "--resident-file"]
        assert not any(static_markers.values())

        plain_resident = root / "04_居民档案" / "虚构未知参数居民.md"
        write(plain_resident, resident_text("虚构未知参数居民"))
        proc = run_cli(
            script,
            "init-resident",
            "--resident-file",
            "04_居民档案/虚构未知参数居民.md",
            "--resident-id",
            "RESIDENT-999",
        )
        assert proc.returncode == 2
        assert "unrecognized arguments" in proc.stderr
        assert "--resident-id" in proc.stderr

        prewritten = root / "04_居民档案" / "虚构预写系统区居民.md"
        write(prewritten, resident_text("虚构预写系统区居民", "RESIDENT-999"))
        before = prewritten.read_bytes()
        before_hash = digest(prewritten)
        proc = run_cli(
            script,
            "init-resident",
            "--resident-file",
            "04_居民档案/虚构预写系统区居民.md",
        )
        assert proc.returncode == 2
        assert "未经登记的系统状态" in proc.stderr
        after = prewritten.read_bytes()
        after_hash = digest(prewritten)

        if proc.returncode == 2 and "未经登记的系统状态" in proc.stderr and after == before:
            behavior = "拒绝未经登记的 RESIDENT-999"
            exposed_path = False
        elif "未改写文件：RESIDENT-999" in proc.stdout and after == before:
            behavior = "直接沿用预写的 RESIDENT-999"
            exposed_path = True
        elif "RESIDENT-999" not in prewritten.read_text(encoding="utf-8"):
            behavior = "重新分配其他 RESIDENT-ID"
            exposed_path = False
        else:
            behavior = "产生未归类结果"
            exposed_path = False

        print(f"L｜STATIC｜prepare 参数：{','.join(prepare_parameters)}")
        print(f"L｜STATIC｜init-resident CLI 选项：{','.join(options)}")
        print("L｜STATIC｜环境变量/配置/计划文件 ID 输入渠道：未发现")
        print("L｜PASS｜未知参数 --resident-id RESIDENT-999 被拒绝，返回码 2")
        print(f"L｜OBSERVED｜预写合法系统维护区：{behavior}")
        print(f"L｜OBSERVED｜文件字节未变：{after == before}；SHA-256 未变：{after_hash == before_hash}")
        print(f"L｜ID_CONTROL_PATH_EXPOSED｜{exposed_path}")
        return 42 if exposed_path else 0


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_m(source: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="community-ai-real04-m-") as temp_name:
        root, script = make_root(temp_name, source)
        module = load_module(script, "workbench_phase1_m_under_test")
        target = root / "04_居民档案" / "虚构替换后验证失败居民.md"
        other_resident = root / "04_居民档案" / "虚构其他居民.md"
        write(target, resident_text("虚构替换后验证失败居民"))
        write(other_resident, resident_text("虚构其他居民", "RESIDENT-710"))
        before_bytes = target.read_bytes()
        before_hash = digest(target)
        before_document = module._read_document(root, target)
        before_region = module._parse_resident_region(before_document.text)
        before_tree = tree_snapshot(root)
        real_replace = module.os.replace
        replace_calls = 0

        def fail_after_first_replace(source_path: Path, target_path: Path) -> None:
            nonlocal replace_calls
            replace_calls += 1
            real_replace(source_path, target_path)
            if replace_calls == 2:
                Path(target_path).write_bytes(Path(target_path).read_bytes() + b"forced-validation-failure")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(module.os, "replace", new=fail_after_first_replace):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = module.main(
                    [
                        "init-resident",
                        "--resident-file",
                        "04_居民档案/虚构替换后验证失败居民.md",
                    ]
                )

        after_bytes = target.read_bytes()
        after_hash = digest(target)
        after_document = module._read_document(root, target)
        after_region = module._parse_resident_region(after_document.text)
        after_tree = tree_snapshot(root)
        residuals = list(root.rglob("*.wbp1-*"))
        lock_exists = (root / ".workbench-phase1.lock").exists()
        assert return_code == 2
        assert replace_calls == 4
        assert after_bytes == before_bytes
        assert after_hash == before_hash
        assert after_region == before_region
        assert not residuals
        assert not lock_exists
        assert after_tree == before_tree
        assert "已恢复" in stderr.getvalue()
        print("M｜PASS｜命令返回失败（2）")
        print("M｜PASS｜目标文件最终字节及 SHA-256 与执行前完全一致")
        print("M｜PASS｜重新解析结果与执行前一致")
        print("M｜PASS｜无 staged、backup、lock 残留")
        print("M｜PASS｜其他业务文件没有变化")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="REAL-04 L/M 定向验证")
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--case", required=True, choices=("L", "M"))
    args = parser.parse_args()
    source = args.script.resolve()
    if not source.is_file():
        parser.error(f"被测脚本不存在：{source}")
    return run_l(source) if args.case == "L" else run_m(source)


if __name__ == "__main__":
    raise SystemExit(main())
