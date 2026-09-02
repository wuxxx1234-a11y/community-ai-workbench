# 社区 AI 工作台

一个面向单名社区工作人员的本地工作台原型。它让 AI 能够在既有工作流程中持续理解事项变化，同时坚持来源可核对、事实不升级、确认后执行和最小认知转换。

当前版本为 **V0.1**。项目以 Markdown 和 Office 文件作为业务载体，用 Python 完成需要确定性的编号、关联、归档和附件操作。

## 设计原则

- 不新增工作负担：工作台寄生在原本就会发生的社区工作中。
- 最小认知转换：能保存来源就不先归纳，能确定性执行就不让模型判断。
- 人工确认优先：居民建档、事项关联和文件归档均不能由 AI 自动升级。
- 来源与事实分离：居民、物业、部门等主体的陈述不会自动成为客观事实。
- 时点输出冻结：已经确认的周总结和阶段材料作为历史快照保存。

详细业务规则见 [AGENTS.md](AGENTS.md)，本地工作入口见 [README_先看这里.md](README_先看这里.md)。

## 代码组成

- `scripts/workbench_phase1.py`：分配和校验 MATTER-ID、RESIDENT-ID，并执行经工作人员确认的居民—事项双向关联。
- `scripts/workbench_archive.py`：根据已确认的 JSON 计划确定性归档文件，并逐项报告结果。
- `scripts/matter_attachments.py`：管理事项附件相关的确定性操作。
- `scripts/test_*.py`：覆盖编号、注册表、关联、归档及附件流程的自动化测试。

程序目前只使用 Python 标准库，不需要安装第三方 Python 包。

## 运行环境

- Python 3.10 或更高版本
- Windows 为当前主要验证环境

运行完整测试：

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```

查看主要命令：

```powershell
python scripts/workbench_phase1.py --help
python scripts/workbench_archive.py --help
python scripts/matter_attachments.py --help
```

## 数据与隐私边界

这个仓库只保存程序、测试、规范和说明，不保存实际社区业务数据。

编号业务目录（事项台账、居民资料、居民档案、待办、会议材料、周总结等）、运行时计划、锁文件和本地配置均由 `.gitignore` 排除。测试数据必须是虚构或完全去标识化的数据。

请勿把真实居民姓名、联系方式、住址、证件材料、诉求记录或其他敏感资料提交到 Git 历史中。即使远程仓库设为 private，也不能替代组织内部的数据安全与授权要求。

## 当前范围

V0.1 面向单工作人员、单独工作区，不提供多人共享、跨工作人员事项合并、自动居民画像、事项重要性评分或自动事实升级。

项目是否成功只看一个朴素标准：正常使用两周以后，工作人员是否仍会主动打开它。
