# 旧字幕工厂（受保护，逐字复制迁入）

迁入方式：**复制**自 `C:\Users\Administrator\PycharmProjects\PythonSRT` 根目录，原件未改动，
仍以 C 盘原树为准。

## 为什么放在仓库根目录（平铺），而不是单独子目录

旧模块之间使用**平铺 import**（例如 `from subtitle_factory_core import LogicCore`），
而 `word_video/jobs.py` 也直接 `from subtitle_factory_api import load_words, selection`。
平铺放回根目录可以**一行代码都不改**地保持旧链可用（迁入验证时实测：
放进 `legacy_subtitle/` 会让 4 个 jobs 测试直接 `ModuleNotFoundError`）。

| 文件 | 作用 |
|---|---|
| `Words_SRT.py` | 旧 GUI 主程序（PyQt6） |
| `subtitle_factory_core.py` | 字幕生成核心（受保护，不改） |
| `subtitle_factory_api.py` | 词表读取 `load_words` / `selection` / `execute` |
| `subtitle_factory_cli.py` | 旧 CLI（C 的 W10 适配器以独立子进程调用它） |
| `subtitle_factory_ui.py` | 旧 UI |
| `word.py` | 旧词表处理 |
| `legacy_tests/` | 随源码迁入的旧测试（**不在默认套件内**） |

## 旧测试状态（2026-09-16）

| 测试 | 状态 |
|---|---|
| `legacy_tests/test_repair.py` | **待验**：需要 PyQt6（锁定 venv 未装，新软件用 PySide6） |
| `legacy_tests/test_ui_contract.py` | **待验**：还需要审计基线目录 `ui_redesign_20260912_151831/baseline/`（未迁入） |
| `legacy_tests/test_ai_interface.py`、`test_glass_assets.py` | **待验**：依赖未迁入的 `liquid_glass` 与 AI 接口实现 |

原件仍在 C 盘原树，M0 时已在原树跑过并通过（156 passed 的那次运行）。
是否需要在本仓库补齐 PyQt6 与审计基线，由 H0 在 W10 前决定（新增依赖只能由 H0 更新）。

## 纪律

- **不改核心**：W10 只做"独立子进程 + 新入口"，不修改本目录实现。
- 不得删除这些文件，也不得改写 C 盘原树。
