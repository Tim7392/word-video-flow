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
| **P1 早期三词候选包（H0 亲自跑）** | **完成（机器侧）**：在迁入后的仓库用现有引擎跑真实媒体三词（151-153，job `wv-c31788ea086725051de6f5d4`，本地缓存音频、**未联网未调 TTS**），产出 31 个产品文件：1080p/h265/60fps/12.467s 带透明片头 MP4（render 11.4s / draft 1.5s / speech 0.5s）+ 五轨 SRT + 可编辑剪映草稿（结构自检 structural_ok，17 个资源文件）。目录式候选包（**硬链接，未复制字节**，`fsutil hardlink list` 已核）：`out\candidates\p1-三词候选包\` |
| 独立验收（非自检） | `tools\m0\accept_range.py` 八面 **PASS**、`failures={}`；`--pixels all` 11 个探针 `problem_count=0`，实测空白侧最大着墨 0.0017（阈值 0.015）、着墨侧最小 0.0379（阈值 0.006），两侧余量 4~20 倍。报告：`out\candidates\p1-三词候选包\acceptance-report.json` |
| 迁移/保护核对（H0，真实边界算哈希） | `repo\tools\verify_protection.py` → **PASS**：与 M0 冻结清单逐文件比对 **39/39 字节一致、0 改动、0 缺失**（证明"迁入=复制"）；受保护归档 `范围151-200…\0151-0200` 用其自身 `complete.json` 复核 **172/172 哈希一致**（证明开发活动没有写穿旧归档）。报告：`out\reports\protection-check.json` |
| 第三开发位（C）派工 | W09 早期最小目录式包（成员不装 Python/FFmpeg、不配 PATH 也能跑）：PyInstaller onedir + 包内 ffmpeg + 启动器 + 清洗环境实测（PATH 只留 System32、清 PYTHON*、cwd 在包内），重点实测**打包后 worker 子进程仍能启动**。`control\dispatch\C-1-W09-early-package.md`；pyinstaller 6.22.3 已由 H0 装入锁定 venv |
| 依赖可行性（预览路线去风险） | PySide6 **6.11.2 有 cp310-abi3 wheel，可在 Python 3.14 的锁定 venv 安装**（`pip --dry-run` 实测），无需第二套环境；W04 开工前安装并做 import/QtMultimedia 冒烟。旧方案担心的"PySide6 不支持 3.14"不成立 |
| D 盘占用（08 项） | C 盘余 3.19 GB（紧张，禁止把输出/缓存写 C）；D 盘余 44.93 GB。工作根分布：`runtime` 8.9 GB（其中 **QA 的 tmp 8.7 GB**，是 QA 造坏样本时的批次副本，任务结束后由 H0 回收）、`out` 870 MB、`tools` 241 MB、`repo` 1 MB。已确认 h0 自身 tmp 仅 0.1 MB，无 C 盘泄漏 |
| P1 仍**未验**（不要当成通过） | 真实剪映人工编辑（结构可编辑 ≠ 人打开能改）、听感/音色、目标核显轻薄本、跨机；本包未做真实 TTS 调用 |

### 待决/风险（不阻塞当前开发）
- GitHub owner/repo 仍未提供 → 只本地提交。
- 预览档位（720p30/540p30）未批准冻结，实测后再请 Tim 确认。
- 引擎侧 v2 朗读字段清理策略（Tim 已批准"只清派生朗读字段"）尚未落到代码：
  验收器已实现 v2，**当前引擎产出的 v2 批次会被判 FAIL**，需在下一批真实生产前落地（A 后续任务）。
- 旧字幕工厂测试需 PyQt6 与审计基线，若 W10 要复跑，由 H0 统一加依赖（子 Agent 不得自行 pip）。

## 暂停点（Tim 指示"先暂停"，2026-09-16 约 17:5x）

**已停止**：B（W02）、QA（验收集）、C（W09 早期包）三个子 Agent 的本轮运行已被中断；
A（W01）在本轮之前已自行完成并停在干净工作树。**无遗留 worker 进程**（仅剩与本项目无关的
PID 16876）。未做合并、未推送。

| 分支 | 状态 | 说明 |
|---|---|---|
| `task/A` | **2 个提交，工作树干净**，未合并 | `43da365` + `8b68da1`：`wv-project@1` 最小接口（domain/application/storage）+ 54 个测试通过。A 报告的关键取舍：模板展开为绝对时间（单片段移动不牵连他人，联动必须显式）；教学阶段帧决策沿用旧引擎双算式（实测纯精确十进制会在约 4% 帧边界差 1 帧）。A 无真实媒体证据（未出 MP4/SRT/草稿） |
| `task/B` | **3 个提交，工作树干净**，未合并、**未收到报告** | `e1ba853` 新媒体包（片头策略/流信息/代理/重叠混音）、`d180fb4` 片头音轨统一策略、`975bd4e` 原始音频缓存与加工缓存分离。被中断时尚未自测汇报，**H0 未验收** |
| `task/C` | 无提交，**6 项未提交改动保留在工作树** | `packaging/`、`tests/test_packaging_launcher.py`、`tests/test_packaging_scan.py`、`build-report.json`、`scan.json`、`verify.json`（打包脚本与清洗环境验证进行中） |
| `task/QA` | 无提交，**5 项未提交改动保留在工作树** | `tests/acceptance/`、三个验收测试、`tests/conftest.py` 改动（坏样本矩阵移植中） |
| `main` | `ce828d7`，工作树有 2 项未提交（H0 的批次比较工具） | 见下一次提交 |

**本轮失败并被如实记录的一步**：H0 尝试"离线复现 50 词归档批次"做迁移等价性，
命令用了相对路径 `word_video_cli.py` 而工作目录不在仓库内 → `submit` 直接失败（id 为空），
后续轮询空转约 10 分钟并超时；**没有产生任何批次产物，也没有调用 TTS**。
需要重试时须用绝对路径或在 `repo` 目录下执行（`tools\build_replay_request.py` 已就绪：
50 词 / 150 条缓存音频 / 0 缺失）。

**恢复清单（按顺序）**：① 合并 `task/A`、`task/B` 到 main 并复跑集成回归（B 的报告要补）；
② 让 C、QA 提交或说明未提交改动；③ A-2 v2 朗读字段清洗（派工单已就绪）；④ 人工剪映验收卡；
⑤ 50 词复现等价性；⑥ GitHub owner/repo 到位后再推。QA 的 `runtime\tmp\QA` 占 8.7 GB
（可重建的归档副本），恢复时或确认不再需要后再回收。
