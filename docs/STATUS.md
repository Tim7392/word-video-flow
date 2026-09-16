# STATUS

一行一条，最新在下。格式：日期 | 任务 | 状态 | 证据 | 未验/下一步。

## 2026-09-16

| 项 | 内容 |
|---|---|
| W00 工作根与基座 | **完成（本地）** | WORK_ROOT = `D:\1\1-AI_workflow\word_video_flow`；建立 `repo/ worktrees/ tools/ data/ runtime/ logs/ out/`；源码复制迁入（`word_video/` 16 文件、`tests/` 19 文件、CLI/打包/依赖清单、旧字幕工厂 6 文件平铺在仓库根目录、`tools/m0/` 367 文件）；Git 本地基线建立（此前本机**没有任何** Git 版本管理：`PythonSRT` 无 `.git`，工作区 `.git` 是空壳） | 原树/旧归档/旧草稿未改；GitHub 未绑定，未 push |
| 三项待决 | 2 项补齐 | ① WORK_ROOT 已绑定（Tim 选定）；② TTS 单位 = **20000 次请求**，且 Q03 为 Q00 后续授权，允许真实调用；③ GitHub owner/repo **仍未提供** → 只本地 commit | 720p30/540p30 预览档位仍未批准冻结，实测后再请 Tim 确认 |
| R1.1 原则合并 | **完成** | 合并范围：`AGENTS.md`（现场路径/命令/边界/底线）、`docs/DIVISION.md`（分工与并行规则）、`docs/ARCHITECTURE.md`（架构不变项 + 权威来源）、`control/tasks.json`（W00–W11 静态清单）、`control/prompts/{H0,A,B,QA}.md`（角色提示词，含核心原则与禁止事项）；包内模板字段"实际工作根/测试命令/目标仓库"已填实 | 本轮**无活跃子 Agent**（尚未派工），故"增量同步"不适用；新任务直接使用更新后的角色提示词 |
| 环境 | 完成 | `runtime\venv` 锁定解释器 + `requirements-word-video.txt`（+pytest/pillow）；FFmpeg/FFprobe 走开发机既有 `C:\Users\Administrator\.local\bin` | 成员机不装 Python/FFmpeg 由候选包解决（W09） |
| 未验项 | 如实记录 | 未做真实剪映人工编辑；未在目标核显机验证；未做真实 TTS 调用（授权已给，等首批真实三词/50词任务）；未做跨机验证；无第二台电脑 |

## 下一步（第一波）

1. H0：子 Agent worktree 写入隔离探针（两轻量任务），确认隔离后并行，否则单写者+只读并行。
2. A=W01 唯一工程与编辑命令；B=W02 旧视频最小修复与媒体链；QA=坏样本验收集。
3. H0 同时准备三词真实预览素材（1080p 背景 + 透明片头 + 已有音频）。

## 第一波执行记录（同日）

| 项 | 结果 |
|---|---|
| 子 Agent 隔离探针 | **完成**：两个轻量 Agent 实测默认 cwd = `D:\1\1-AI_workflow`（**不是**各自 worktree），但用绝对路径可写自己的 worktree，无授权提示、无 sandbox 拒绝；互不串写（A 目录只有 probe-a.txt，B 只有 probe-b.txt）。结论：**可以做 worktree 并行，但派工单必须强制"只用绝对路径写入、禁止写 `D:\1\1-AI_workflow` 下任何位置"**——已写入 `control/prompts/{A,B,QA}.md` 与三份派工单 |
| 派工 | A=`task/A` W01（commit 1b7a715e）、B=`task/B` W02（5141beba）、QA=`task/QA` 坏样本（3edbd6f6）；基础 commit 均为 `966c683`；C 本波只读，不开第二个写进程（上限 3 开发 + 1 QA） |
| 三词真实夹具 | 建于 `data\fixtures\`：`p1-有片头.json`、`p1-无片头.json`、`p1-无声片头.json`，输出指向 WORK_ROOT `out\samples\*`，各 9 条真实缓存音频（本地 provider，无网络），11/10/11 个引用路径全部存在（缺失=0） |
| 迁移后测试套件 | 引擎套件（`repo\tests`）可跑；旧字幕工厂测试（`test_repair.py`、`test_ui_contract.py`、`test_ai_interface.py`、`test_glass_assets.py`）**移入 `legacy_tests\` 并标记待验**：需要 PyQt6（锁定 venv 未装，新软件用 PySide6）与审计基线目录 `ui_redesign_20260912_151831\baseline\`（未迁入）。原件仍在 C 盘原树，M0 已在原树跑通过（156 passed 那次）。`pytest.ini` 的 `testpaths=tests` 使默认套件只覆盖本产品引擎 |
| 迁移期发现的真实破坏（已修） | 旧字幕工厂 6 文件最初被我放进 `legacy_subtitle\` 子目录，**导致 4 个 jobs 测试直接 `ModuleNotFoundError: subtitle_factory_api`**（`word_video/jobs.py` 用平铺 import 读 `load_words`/`selection`，旧模块之间也是平铺 import）。修法：按原树布局**平铺回仓库根目录**，一行旧代码都不用改（整体收益：零改动保持旧链可用；代价：仓库根目录多 6 个文件）。已写入 `docs/LEGACY_SUBTITLE.md` |
| 环境 | `runtime\venv` 建立成功（Python 3.14）：pyjianyingdraft 0.3.0、pymediainfo、python-docx、lxml、pillow、pytest、numpy、uiautomation；**PySide6 未装**（`--dry-run` 解析结果见 `logs\venv-setup.log`），W04 预览开工前由 H0 决定是否安装及版本 |

### 待决/风险（不阻塞当前开发）

- GitHub owner/repo 仍未提供 → 只本地提交。
- 预览档位（720p30/540p30）未批准冻结，实测后再请 Tim 确认。
- 引擎侧 v2 朗读字段清理策略（Tim 已批准"只清派生朗读字段"）尚未落到代码：
  验收器已实现 v2，**当前引擎产出的 v2 批次会被判 FAIL**，需在下一批真实生产前落地（A 后续任务）。
- 旧字幕工厂测试需 PyQt6 与审计基线，若 W10 要复跑，由 H0 统一加依赖（子 Agent 不得自行 pip）。
