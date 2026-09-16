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
| 保护核对（H0，真实边界算哈希） | `repo\tools\verify_protection.py` → **PASS**：① 受保护旧字幕核心 6 文件与 C 盘原树**逐字节相同 6/6**（迁入是复制，W10 只做外层适配）；② **全部 8 个已交付归档批次 172/172 哈希一致、0 改动、0 缺失**（`0001-0050`…`0651-0700`），证明今天的开发活动没有写穿历史产物。报告：`out\reports\protection-check-all.json`。**检查范围已按事实调整**：迁移期的"引擎源码 = M0 冻结版"门禁已退役（`word_video/**` 现在本来就该改，`media.py` 也被 B 有意改名为 `media/core.py`），改为只守"旧核心不变 + 归档不变" |
| 第三开发位（C）派工 | W09 早期最小目录式包（成员不装 Python/FFmpeg、不配 PATH 也能跑）：PyInstaller onedir + 包内 ffmpeg + 启动器 + 清洗环境实测（PATH 只留 System32、清 PYTHON*、cwd 在包内），重点实测**打包后 worker 子进程仍能启动**。`control\dispatch\C-1-W09-early-package.md`；pyinstaller 6.22.3 已由 H0 装入锁定 venv |
| 依赖可行性（预览路线去风险） | PySide6 **6.11.2 有 cp310-abi3 wheel，可在 Python 3.14 的锁定 venv 安装**（`pip --dry-run` 实测），无需第二套环境；W04 开工前安装并做 import/QtMultimedia 冒烟。旧方案担心的"PySide6 不支持 3.14"不成立 |
| D 盘占用（08 项） | C 盘余 3.19 GB（紧张，禁止把输出/缓存写 C）；D 盘余 44.93 GB。工作根分布：`runtime` 8.9 GB（其中 **QA 的 tmp 8.7 GB**，是 QA 造坏样本时的批次副本，任务结束后由 H0 回收）、`out` 870 MB、`tools` 241 MB、`repo` 1 MB。已确认 h0 自身 tmp 仅 0.1 MB，无 C 盘泄漏 |
| P1 仍**未验**（不要当成通过） | 真实剪映人工编辑（结构可编辑 ≠ 人打开能改）、听感/音色、目标核显轻薄本、跨机；本包未做真实 TTS 调用 |

## 恢复后第一轮集成（2026-09-16 晚）

| 项 | 结果 |
|---|---|
| 合并 | `task/A`（W01）与 `task/B`（W02）已并入 main（`09c1d3d`、`71602ae`），**无冲突**；集成回归 **215 passed / 49 subtests**（合并前 111/39） |
| 合并后三词真实验收 | 新键重跑 151-153（job `wv-3c9fef282af48ae118849a82`，17.4s，render 10.75s）→ 独立验收器 **PASS**、像素 0 问题；与合并前批次比对 **timeline 0 差异、五轨 SRT 逐字节相同** |
| **50 词离线复现（迁移等价性，H0 亲跑）** | 用归档 0151-0200 的 150 条**真实缓存原件**（provider.kind=local，**零网络零 TTS**）在 `out\samples\p1-50词复现-local` 重跑：68.9s（render 33.6s / draft 18.8s / speech 8.7s）、**172 文件**；与归档比对 **timeline 0 差异、五轨 SRT 全部 IDENTICAL**；独立验收器八面 **PASS**（`failures=[]`，计时 150 段 0 问题、`data_missing=0`、完整性 172/172 已列、音画 e2e `problem_count=0`、混音 182.6s 与 MP4 182.613s 对齐、片头有声）。**结论：合并后的引擎可逐帧复现历史归档**，B 担心的"旧 raw 元数据缺陷导致段长变化（59→60 帧）"在这批 150 条上**没有出现** |
| 接口语义坑（已记录） | `prepared_speech` 的语义是"调用方已给**加工后**音频"：喂未变速原件会被 `draft.py` 正确拒绝（`Speech exceeds allocated timeline stage`，8s 失败、无产物）。**复用已有音频的正规入口是 `provider.kind=local`**（引擎自己探测原件时长并变速加工，时间线指向加工后的 `prepared.wav`）。W07 的旧配音导入必须走后者；建议后续把该错误信息改成可诊断的提示（排入 W06/W07） |
| C 的 W09 早期包 | 已交付：`out\packages\word-video-member-0.1.0\`（179.9MB，179 文件），清洗环境（PATH 仅 System32、无 PYTHON*）`doctor` 1.00s 单行 JSON、三词作业 11.5s、worker 独立进程 pid 61056、29 文件 SHA256 复读一致。**但构建自合并前的引擎**，已派 C-2 重建到合并后并交 QA 独立复验 |
| 待决（H0 已决定，记此） | B 的缓存 token 方案变化使**旧缓存目录全部成为孤儿**（实测旧 token 命中 0/150）。决定：**做一次性"旧缓存导入"**（用旧目录里现成的 `original.*` 按内容身份喂 `provider.local`，零网络、由引擎重新测长并加工），收益=复用约千条真实 TTS 音频且不重扣额度、代价=一个导入工具+校验；归 W07，由 B 实现。依据：Tim 定"优先已有音频"、W07 本就有旧配音导入要求，且上面的 50 词复现已证明该机制可用 |
| 进行中 | A=A-2 v2 朗读字段清洗；B=W03 排版与三产物；C=C-2 成员包重建 + W04 预览；QA=验收集收尾（含 W01 反例接线） |

| 50 词复现**逐词像素**验收 | `--pixels all` 在同一批复现产物上重跑：**151 个探针 0 问题**，实测空白侧最大着墨 0.0061（阈值 0.015）、着墨侧最小 0.0206（阈值 0.006）——**与 M0 在归档原件上测得的数值完全一致**，证明合并后引擎连像素表现都复现了归档；另附计时 150 段 0 问题、音画 e2e 0 问题 |
| 剪映人工验收准备 | 已把**合并后引擎**产出的草稿复制到剪映草稿目录并 `relocate` 成功：`D:\jianying\JianyingPro Drafts\单词速记_P1三词待验_20260916`（21 文件，`draft-state`: PLAIN_JSON / relocatable=true / editable_in_jianying=true）。人工验收卡已改指向该副本；M0 旧副本与候选包内草稿保留为等价备选 |
| **真实 TTS 首次调用（本轮唯一真实服务调用）** | 用内置探针 `word_video_cli.py tts-audition`（设计好的真实服务验证入口）实测：路由 `volcengine_legacy`，三个音色各生成 1 条——`BV503_streaming`（女声，2.664s）/`BV504_streaming`（男声，2.952s）/`BV406_streaming`（网文解说，3.024s），2.8s 完成，`synthesis_complete: true`，**消耗 3 次请求**（额度 20000 次口径）。凭据只从**用户级**环境注入子进程（我的会话进程未继承），**没有写入任何日志/仓库/报告**。产物在 `runtime\tmp\h0\audition-r1`（不进 Git）。`listening_parity: PENDING_USER_AUDITION`——**机器不评价音色好不好听** |
| 接口冻结：`LayoutSurface`（B，`2c92b9f`） | 已转达 C 并要求 W04/W06 复用（不许第二套排版）。冻结点：坐标是画布比例 0..1、字号按 `height/2160` 缩放（小画布预览与 4K 成片同相对位置）；一个 placement = 一个文字块（折行后以 anchor 为中心）；**溢出是数据不是异常**（`require_fit()` 才拒绝），水平安全边距 0.04 硬规则、垂直允许块高部分越界（依据：既有 title anchor 本就在画面上边之上 0.005）。B 自测 17 项全绿；如需加字段须在导出器开工前提出 |
| 域模型缺口与 H0 裁决 | B 报：`Project` 只能用 `intro_s` 表达片头**时长**，没有片头**素材**角色（`CLIP_ROLES` 无 intro 项），所以"含真实透明片头的工程"无法只靠 `project.json` 表达。**裁决**：① 本轮允许 B 先把片头作为**导出参数**接住，保证 P1 三产物当天能打通（时间线仍是唯一的，片头只是该阶段素材来源）；② 随后由 A 把 `intro` 作为工程的一个 layer/clip 角色纳入 schema（版本化、向后兼容旧工程由 A 定口径），完成后 B 把导出器改成**只读计划**，消除这处临时分歧；③ 这条分歧必须在本文件保留到 ② 完成，不允许默默消失 |

| **真实 TTS 端到端三词（批次规模验证）** | `p1-真TTS-151-153`（job `wv-810109d7b9ff0a6672d2c6ad`，`provider.kind=volcengine_original` + 三音色确认）：**22.9s 完成、31 文件**，speech 阶段 2.4s → 工作根内新建 **9 个 token**（每个含 `original.volcengine_legacy` + `prepared.wav` + `complete.json` + `prepared.json`，全部为本次合成，时间戳可核）；时间线音频路径全部落在工作根作业缓存内。独立验收器 **PASS**（`failures=[]`、像素 10 探针 0 问题、计时 9 段 0 问题、音画 e2e 0 问题、MP4 12.48s、片头有声）。**消耗 9 次请求**（累计 12/20000）。参考价值：本批三段帧数（59/58/79、51/50/77、62/65/135）与归档批次**完全一致**，说明同文本同音色的真实合成时长稳定 |

| **A-2 v2 朗读字段清洗（已合并 `294b9fa`）** | 新增 `word_video/text_policy.py`（纯函数）＋ `LessonSpec.spoken_policy`（默认 **v2**）＋发布清单盖章；显式 `entries` 的朗读文本为权威不清洗（记 `supplied`）。A 自测 21 项、全量回归 236 passed；**A 的 v2 批与 legacy 批分别用 `--cleaning v2`/`--cleaning legacy` 判，两次 verdict=PASS**。回归还抓出并修掉一个真实缺陷（submit 冻结 `asdict(lesson)` 后 work 回放要能 round-trip），已留反例 |
| 口径核对（H0 工具，取代 M0 的 171） | `tools/reconcile_cleaning_counts.py` 实测 1654 行：**v2 ≠ 旧核心 `d_c` = 291 行**、v2 ≠ 验收器 legacy = **290 行**、验收器 legacy ≠ 旧核心 `d_c` = **4 行**（#705/#1228/#1440/#1626，旧核心词性表更窄所致），清洗后仍含 `&`/`/` = **0 行**。M0 记录的"171"**复现不出来**（A 试了 11 种口径也没有），以本次三口径实测为准。**成本含义**：291 行只在**重做已交付范围**时才需重新合成；201-500/701-1500 本来就没有缓存，v2 不额外花钱。A 另纠正派工单一个假设：**清洗本身不改中文段时长**（汉字数不变），时长变化只来自新文本→缓存未命中→重新合成 |
| **QA-2 独立复验（已合并 `e69b50a`）** | ① **成员包 0.2.0 独立复验**：受限环境（PATH 仅 System32+包内 ffmpeg、无 TTS 凭据、cwd 在包内、请求由 QA **自己从归档派生**）跑 50 词 → `generated`、**172 文件**、92.8s；`accept_range --pixels all` **PASS**（172 哈希全对、五轨 100/50/50/50/50、草稿 13 轨 50 词逐段、150 阶段节奏复算、包络 r=1.0000、像素 151 探针 0 问题）；草稿 **0 悬空 material_id、0 处指向只读归档**；包内扫描**带值密钥/媒体/整表词表/音频缓存/用户数据全部 0 命中**（29 条十六进制串与 1 条 private_key 逐条给理由：FFmpeg 自带测试证书、PyInstaller 常量、公开 sha256）。② **归档复现独立判据**：timeline 逐字段 **0 差异**、150 条音频阶段 0 差异（仅路径不同，逐条列出）、五轨 SRT **逐字节 identical ×5**、`complete.json` 172 条 sha256 全对、10 词×3 相位 60 帧**带均值最大差 0.0**（阈值 2/255）、像素读数与归档报告完全相同 |
| 集成纪律修复（H0） | QA 的重验收默认被执行，把"快速回归"拖到 **831s**（违反"开发随手可跑"）。`pytest.ini` 改为 `addopts = -q -m "not acceptance_media"`：默认套件回到 **284 passed / 18 deselected / 49 subtests / 64.7s**；重验收显式 `-m acceptance_media` 跑（18 项，约 13 分钟，独占槽位） |
| QA-2 新发现（已转 C） | 子进程若缺 `SystemRoot`/`windir`，**包内 exe exit 1 且 stderr 全空**，与"包坏了"无法区分；成员机正常有这些变量，但 C 的 `verify_member_package.py` 应显式补齐并记录。另：包 `VERSION.txt` 记 `source_commit=da04a820 / source_dirty=true`，**无法与某个 commit 一一对应**，C 重建时需修正 |

| **P3 里程碑：首批 v2 真实生产批次 201-250（H0 亲跑）** | 真实 TTS 端到端出齐三产物：job `wv-3adc97b2f61af8b7aae4ab94`，**74.8s**（speech 12.7s 真实合成 150 条 / render 32.6s / draft 16.2s），**172 文件**，`timeline.json` 盖章 `spoken_policy: "v2"`。独立验收器 `--cleaning v2 --pixels all` → **verdict PASS / failures=[]**：完整性 172 文件 0 问题、计时 **150 阶段 `data_missing=0` 0 问题**、音画 e2e 0 问题（MP4 172.843s ↔ mix 172.833s、包络 r=1.0、片头有声 1.867s）、**逐词像素 151 探针 0 问题**（空白侧 0.0037 / 着墨侧 0.0228）、草稿 0 问题、五轨 SRT 齐（4289/2141/2482/2826/2599 字节）。**消耗 150 次请求（累计 162/20000）** |
| v2 策略"真的作用到产物"（H0 工具核对） | `tools/check_batch_policy.py` 对 201-250 实测：50 词中 **3 个词的 v2 朗读文本与历史 `d_c` 不同**（#223 dynamic、#234 characteristic、#243 input，均为去掉派生连接符的差异）；产物 SRT 05 里 **3 条 v2 文本全部命中、历史文本 0 命中**，清单 `spoken_policy=v2`。→ 清洗策略不只是单测通过，**确实落到了成片字幕与合成文本上**（这也补上了 A 报告的"v2 文本从未真实合成过"缺口） |

### 待决/风险（不阻塞当前开发）
- GitHub owner/repo 仍未提供 → 只本地提交。
- 预览档位（720p30/540p30）未批准冻结，实测后再请 Tim 确认。
- 旧字幕工厂测试需 PyQt6 与审计基线，若 W10 要复跑，由 H0 统一加依赖（子 Agent 不得自行 pip）。
- 片头"导出参数 vs 工程 schema"的临时分歧仍未消除（见上一节裁决），等 A-4 把 `intro` 纳入 schema 后由 B 改为只读计划。
- `legacy` 口径与旧核心 `d_c` 的 4 行差异只影响"用旧核心产出的、含这 4 行的批次"被我们验收器判定的场景；已交付的 1-200/501-700 不含这 4 行（M0 已验 PASS），**不在兼容承诺内**的范围需要时再单独定。

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
