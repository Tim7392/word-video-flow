# 派工单 C-5｜成员包带编辑器（W09 硬化第一步）+ 后续排期

- **基础 commit**：合并后的 main（含 W06 编辑器；H0 会给 hash）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\C`（`git checkout -b task/C5 main`）
- **角色提示词**：`control/prompts/C.md`
- **前置**：W06（你的编辑器）、W04（预览）、W09 早期包

## 为团队解除什么阻塞

成员现在拿到 `out\packages\word-video-member-*` 只会得到**命令行**——而成员要的是**打开软件、导入词表、
看着预览改一改、导出三产物**。编辑器已经做完了，但**成员包里没有它**。本任务把编辑器装进包，
让"成员本机可用"这条从"能跑命令"变成"能开软件"。

## 可改范围（白名单）

- `packaging/**`（构建脚本、启动器、成员说明、验收脚本）
- 为打包需要的**最小**入口调整：`desktop/**` 里新增一个 `__main__` 式入口（不改既有编辑器行为）
- 新增 `tests/test_packaging_editor.py`
- **禁止改**：`word_video/**`（A/B）、`preview/**` 既有行为、旧字幕核心、`AGENTS.md`、`control/**`、
  `docs/**`、其他 worktree、旧归档

## 要求

1. **包里带编辑器**：PyInstaller onedir 打包包含 PySide6 与 `desktop` 入口；
   启动器 `单词视频.cmd` **默认打开编辑器**；**同时保留命令行入口**（Agent 要用），
   例如另给一个 `.cmd` 或在启动器里用参数区分——两种用法都写进 `README-成员使用.md`。
2. **不要求成员装任何东西**：运行时、Qt 平台插件、ffmpeg 都在包内；
   `PATH` 只加包内目录；`TEMP/TMP` 指向包内或用户指定目录（沿用你已有的做法）。
3. **`VERSION.txt` 必须能对应到 commit**（干净树构建；dirty 要列出具体文件），沿用你 C-2 已修好的口径。
4. **包内不得含**：密钥、真实素材、音频缓存、全量词表、无分发依据字体（沿用你已有的扫描器，重跑一次）。
5. **不要为打包改生产代码**：如果编辑器在冻结环境下有导入/路径问题，先报 H0，别在 `word_video/**` 或
   `preview/**` 里塞打包特判。

## 验收（必须真跑并贴结果）

1. **清洗环境 GUI 冒烟**：`PATH` 只留 `C:\Windows\System32` + 包内、清空 `PYTHONHOME/PYTHONPATH`、
   无 TTS 凭据、`cwd` 在包内、`QT_QPA_PLATFORM=offscreen` → 通过启动器打开编辑器，
   **完成一次三词导入→导出**（可脚本驱动），产物交 `tests\acceptance\accept_range.py --cleaning v2 --pixels all` 判定；
   记录启动耗时与导出耗时。
2. **CLI 仍可用**：同一清洗环境下用命令行入口跑一次三词（或 `doctor`），证明两条路都在。
3. **包内容扫描**：文件数/体积 + 无密钥/素材/词表/缓存（贴扫描输出）。
4. **VERSION ↔ commit**：贴 `git log` 对齐证据。
5. 全量回归：`python -m pytest tests`（默认口径；重验收用 `-m acceptance_media`，别默认跑）。

## 后续排期（本单交完后）

- **C-6（W10 旧字幕轻量适配）**：派工单已就绪 `control\dispatch\C-4-W10-legacy-adapter.md`。
- **C-7（预览 720p30 抽帧）**：冻结档位是 720p30，但你实测"画布 720p、解码仍 60fps"。
  单派时要求：**默认按 30fps 抽帧、保留 60fps 能力（可配）**，记录首帧/seek/CPU 的实测差异，
  并写明"目标核显机未验"；改动落在 `preview/**`，需带 W04 既有验收的回归。

## 禁止事项

只用绝对路径写文件；不新增依赖（PySide6 已在锁定环境）；不改生产代码让打包通过；
不 force push、不 merge；不递归派子 Agent；GUI/长测试独占本机槽位。

## 报告格式（只报这四项）

改了什么（含 commit hash） → 测了什么及实际结果（含清洗环境证据与产物验收 verdict） →
未验证什么（例如未在成员机、未做人工可用性） → 下一步/回滚 commit。
