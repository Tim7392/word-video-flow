# 单词教学视频制作软件 — 项目执行规则（现场版）

本文件是 R1.1 原则补丁合并进实际仓库后的现场规则。它取代包内模板；模板里"待现场确认"的
字段已按下表填实。**保留已有代码、路径、进度和已确认决定，不用模板覆盖。**

## 核心工程原则（每次派工必须传入）

**从第一性原理出发，顾全大局，整体最优优先。** 先确定团队成员/Agent 的真实目标、事实和约束，
再选最小充分方案；可靠复用，不做无依据的抽象、重复实现或推倒重写。

整体判断包含使用与返工、端到端等待、内存/计算/磁盘/TTS、可靠性、安全、开发与长期维护。
不为单模块更快、更美或更少代码把成本转嫁给系统及成员；有依据的局部取舍可以接受，
授权与已批准验收不可暗改。不能证明需要的大改先不做，不为代码整洁顺便整理无关模块。

H0 每次派工传入本原则及整体目标，按依赖/关键路径编排；各角色只改任务相关内容，
QA 复核集成影响。重要取舍在提交或 `docs/STATUS.md` 短记"整体收益/主要代价/验证依据"；
普通修复不增文档、不写长报告、不跑全量检查。

**不以"整体最优"为理由越权、改默认、换声音/字体、删关键验收或降低已批准标准。**

## 执行边界

- 顶层基线：**2026-09-16 R1.1（原则补丁，继承 R1 范围）**；Tim 最新明确指示优先。
- 只在下方 WORK_ROOT 内开发。默认模型，最多 3 开发 + 1 QA，子 Agent 不递归派工。
- 原字幕核心、原入口、旧归档、旧剪映草稿受保护：迁入用复制，不删原件，旧视频只修允许的副本。
  系统改动 / 正式发布 / 签名 / 公开仓库另批。

## 现场路径（实际值）

| 用途 | 绝对路径 |
|---|---|
| WORK_ROOT（Tim 指定 D 盘工作根） | `D:\1\1-AI_workflow\word_video_flow` |
| 仓库主工作树（含本文件） | `D:\1\1-AI_workflow\word_video_flow\repo` |
| 子 Agent 工作树 | `D:\1\1-AI_workflow\word_video_flow\worktrees\<角色>` |
| 非仓库工具（H0） | `D:\1\1-AI_workflow\word_video_flow\tools` |
| 数据/夹具/素材/缓存 | `D:\1\1-AI_workflow\word_video_flow\data\{fixtures,media,wordlists,cache}` |
| 运行时（锁定 venv、临时） | `D:\1\1-AI_workflow\word_video_flow\runtime` |
| 日志 / 输出 | `D:\1\1-AI_workflow\word_video_flow\{logs,out}` |
| 受保护：旧引擎原始树 | `C:\Users\Administrator\PycharmProjects\PythonSRT` |
| 受保护：旧成片归档 | `D:\单词速记自动化_测试归档_0915` |
| 受保护：旧剪映草稿 | `D:\jianying\JianyingPro Drafts` |
| 受保护：M0 验收证据 | `D:\1\1-AI_workflow\word_video_m0` |
| 全量词表（不进 GitHub） | `D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt` |
| 1080p 素材 | `D:\单词速记自动化_测试归档_0915\_prepared-1080p` |

迁入方式：`C:\...\PythonSRT` 的 `word_video/`、`tests/`、`word_video_cli.py`、
`word_video_package.py`、`requirements-word-video.txt` 已**复制**到 `repo\`；
原字幕工厂 5 个模块 + `word.py` 已复制到 `repo\` **根目录**（逐字未改，平铺布局；
原因见 `docs/LEGACY_SUBTITLE.md`：旧模块用平铺 import，放进子目录会让 `word_video/jobs.py`
直接 import 失败）。旧测试复制在 `repo\legacy_tests\`（默认不跑，状态见同文档）。
M0 验收/工具脚本复制在 `tools\m0\`。原件一律不动。

## 环境（只由 H0 更新，子 Agent 不改共享环境）

- 锁定解释器：`D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe`
- FFmpeg/FFprobe（开发机已有，成员机由候选包自带）：`C:\Users\Administrator\.local\bin\{ffmpeg,ffprobe}.exe`
- 依赖清单：`repo\requirements-word-video.txt`（+ pytest / pillow 开发用）
- 子 Agent 从**自己的 worktree** 显式加载源码，不做指向他人代码的 editable 安装；
  各自用自己的测试 tmp。禁止并发 pip/uv 修改共享 venv。

## 实际命令（H0 维护）

```powershell
# 快速回归（在 repo 目录下）
& D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe -m pytest -q tests

# 三词烟测（真实媒体，本地缓存优先，不联网；--db 指到 work root）
& D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe word_video_cli.py `
    --db D:\1\1-AI_workflow\word_video_flow\data\jobs\p1.sqlite3 `
    submit --request D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-有片头.json
```

## 角色边界（详见 `docs/DIVISION.md`）

- **A**：`word_video/{domain,application,storage,cli}` 与公共契约；W01/W05/W08。
- **B**：`word_video/{media,voices,layout,exporters}` 与旧视频修复副本；W02/W03/W07。
- **C**：`desktop/`、`preview/`、`packaging/`、`legacy_adapter/`；W04/W06/W09/W10。
- **QA**：`tests/acceptance/`、性能入口、验收结果；W11 + 每次集成反例。
- **H0**：根配置、锁文件、AGENTS/STATUS、集成分支与 push；W00。

公共 schema 由 A 单写，消费者用同一版本；B 需要 UI 页面时给 C 组件需求，不越界改 desktop。

## 已确认决定（不再重复问）

1. 内部团队使用；成员本机生产；模板/高级模式共用一个工程；三产物 = MP4 + 五类 SRT + 可编辑剪映草稿及所需媒体。
2. 默认点选单片段；单片段移动不默默带动未选对象，整组/联动是显式操作；教学交付拦截缺角色、错误顺序、歧义语音重叠，允许冲突工程保存与修正。
   **2026-09-17 范围裁剪（Tim）**：软件内**不做"可编辑时间线模拟剪映"**（拖动/裁剪/拆分/绑定这些交互下线，见第 9 条），
   本条关于"点选/移动/联动"的口径随之**不再作用于本软件的界面**；改时间、改样式一律在**剪映草稿**里做（草稿可编辑是硬要求，不变）。
3. 最低目标为普通核显轻薄本；剪映会更新：用本机已安装版本探测 + 兼容记录 + 更新后三词复验，不承诺永久支持任何最新版。
4. 保留现有女声/男声/中文音色，优先已有音频；必须有方便的旧剪映配音/音色导入流程；导入已有音频 ≠ 获得新文字合成能力；文字/字体/音色不静默替代。
5. 全部开发、依赖、素材、缓存、日志、输出默认在 WORK_ROOT 内；成员无需安装 Python/FFmpeg 或配置 PATH。
6. 最多 3 开发子 Agent + 1 QA；Harness 现有默认模型/派发方式，不做模型路由或预算审批。
7. 迁入已有成果与原字幕工厂以复制/复用优先；不改字幕核心、不删旧件、不改旧快捷方式；不改系统设置。
8. 必须 GitHub 版本管理，但只推确认仓库中的代码、测试、小夹具和文档；不传密钥、真实素材、用户工程、音频缓存、无分发依据字体。
9. **预览只做"模板效果预览"（Tim 2026-09-17 明确，原话："太卡且占用资源了，现在不做可编辑的时间线模拟剪映了，只做模板效果预览也就是预览文字，单词这些"）**：
   预览**只画文字层**（单词/音标/中文释义的字体、字号、颜色、位置——必须与成片**同一套排版**），背景用**一张静帧**；
   **不解码视频、不建低清代理、不做后台分段编码、不出声（无音频设备与 PCM 时钟）、不做软件内的时间线编辑**；
   重绘按需（位置/样式/尺寸变化才重画）并可低频播放，**不做 20/60Hz 定时器、不每帧新建 Qt 对象**。
   这条**取代**了 ARCHITECTURE 里"连续预览=低清代理+按需解码+统一 PCM 时钟"的旧不变量；
   **三产物（MP4 + 五类 SRT + 可编辑剪映草稿）与命令层不变**，编辑仍在剪映里完成。资源占用与"打开就能看见文字"的时延是这条的验收面。
10. **成员复用剪映排版 = 导入剪映草稿生成模板（Tim 2026-09-17 明确，原话："其他团队成员用的话要支持导入剪映草稿获取排版来生成模板"）**：
    支持**任意剪映草稿**（含成员自己手工做的）读出排版（字体/字号/颜色/位置/粗体）**生成可复用模板**；角色**按内容识别**（英文/音标/中文……），
    **认不出就明确报错并要求指定，不许猜**；同角色样式不一致时**列出全部变体**取多数，**不许静默平均**；我们表达不了的属性（描边/阴影/动画）**如实列为未支持**。
    **字体：模板默认带字体名字（+ 文件哈希），导出模板时可用显式开关把字体文件一起打包**；使用端按名字/哈希解析，
    **找不到或哈希不符就结构化拒绝并给出修法，绝不静默替换**（"字体不静默替换"底线不变）；**工程级 override 仍不允许点名字体**（只有模板可以）。
    **成员包必须自带默认样式所需字体**——现有默认样式引用的是本机路径与剪映缓存字体，别的机器上不存在，这是"别人能不能用"的前提。
11. **派发路由（Tim 2026-09-17 明确）**：**所有开发/QA 子代理一律走 `openrouter` / `stealth/union-alpha`（Union Alpha）**；
    H0 仍在当前模型上做指挥（派工、复核、合并、验收、对外汇报）。**操作事实（H0 实测，别重复踩）**：子代理模型白名单由
    `$DSH_HOME/settings.yaml` 的 `subagent-model-selection.allowedModels` 决定，而它是**"会话创建时"捕获的权威快照**
    （源码 `packages/subagent/tool-subagent/src/model-selection.ts:134`：*Selection authority captured for this Session*）——
    **改配置后必须新开会话（或 fork）才生效；刷新页面无效**（实测 3 次 `child LLM route "openrouter/stealth/union-alpha" is not allowed for this Session`）。

**TTS 授权（Tim 2026-09-16 明确）**：初期真实 TTS 额度 **20000 = 次请求**；**Q03 是 Q00 的后续授权**，
即允许本机真实调用（火山）TTS，按次请求口径计量。授权只放开"真实调用"这一项：
仍不做未知结果的盲重试（一次失败先判类型，不重复扣费），仍不把未公开内部配音通道当默认团队能力。

## GitHub（已绑定，2026-09-16）

- 目标仓库：**`https://github.com/Tim7392/word-video-flow`（私有）**，owner 由 Tim 指定，仓库由 Tim 创建。
- **只推 `main`**（工作分支 `task/*` 留在本地）；H0 统一推送，子 Agent 不 push、不 force push。
- 推送前扫描（已做成常规动作）：只含代码/测试/小夹具/文档；**不含**密钥、真实素材、用户工程、
  音频缓存、无分发依据字体。首次推送实测：228 文件 / 2.15 MB，仅 `.py/.md/.txt/.ini/.gitignore/.json`，
  6 类敏感形态 0 命中，历史从未加入可疑扩展名。
- 提交身份保持仓库本地的 `H0 <h0@local>`（Tim 确认）；不推断、不冒用 Tim 的身份推送。
- 首次推送后核对：`git ls-remote origin -h refs/heads/main` 必须等于本地 `HEAD`。

## 已冻结的档位（Tim 2026-09-16 决定）

- **预览：720p30**（必要时可降到 540p30）；**成片：1080p/4K、60fps 能力不变**。
  预览验收按此档位记录实测；目标机（普通核显轻薄本）未实测前不宣称"低配已验"。
- **生产：暂停**（Tim 2026-09-16 决定）。已交付 201-250、251-300 两批 v2 正式批次即当前产出边界；
  不再开新批次消耗 TTS 额度，**火力转到软件**（W05 协调器/CLI、W06 编辑器、W09 打包硬化、W10 旧字幕适配）。
  需要恢复生产时由 Tim 明确指示；**零网络的本地验证跑（provider.kind=local / 坏样本 / 导入对照）不属于生产，照常进行**。

## 不可踩的底线

- 三产物共用一条时间线；`ass=` 必须始终是滤镜链最后一级（有片头也一样）。
- 字体/音色不静默替换；不为缩短时长剪音频；发布用硬链接不覆盖已发布产物；
  未提交批次进 `recovery/`。
- 每个变更附相关测试；修 bug 先留反例；不降低阈值、不用 SKIP 或旧 PASS 顶替。
- 默认测试不带真实 TTS 凭据且阻断应用网络（Harness 模型网络不受影响）；子进程传参不用 shell 拼串；
  不用 pickle/eval 加载用户输入；ZIP/模板不含可执行脚本；不自动下载媒体 URL。
- 哈希只在真实边界做（资产入库/变化、迁移字节核对、关键运行输入身份、小字体身份、发布包完整性），
  不每次启动、每帧、每条命令全盘验 hash。
- 用户诊断日志不放密钥和完整词表；用户数据不进 GitHub。

## 交付与提交纪律

每个任务只报：**改了什么 → 测了什么及实际结果 → 未验证什么 → 下一步/回滚 commit**。
小改不写长报告；阶段结束一次短汇总。Git 提交小步走；只有改公共接口才更新
`docs/ARCHITECTURE.md`，用户可见影响才更新 `docs/CHANGELOG.md`。

## Antigravity 作为可跟进 worker（Tim 2026-09-18 要求，H0 已落地）
- **入口**：`D:\1\1-AI_workflow\word_video_flow\tools\agy_bridge.py`（包住官方 CLI `agy`，默认模型 `gemini-3.8-flash-high`）。
- **为什么不用 DSH 原生子代理**：`agy` 没有 ACP/协议模式；且 DSH 的 `subagent-acp`/`codex`/`claude-code`/`dsh-sdk` **都是一次性**后端（无 `prepareContinuable`）⇒ 接上也**不能跟进**。
- **持续跟进用法**：`--worker <名字>` 即一条会话（后续调用自动 `--conversation <id>`）；`--list-workers` 查看；`--forget-worker` 重开。状态在 `<允许根>\.agy-bridge\workers\*.json`，会话本体在 Antigravity 侧，跨重启不丢。
- **边界（硬）**：默认允许根 = `D:\1\1-AI_workflow\antigravity`（它自己的工作树）；`--allow-edits` 另有 `AGY_BRIDGE_EDIT_ROOTS` 约束；它**不 push、不改 main**，产物要我复核后合并。
- **它写的报告**：`D:\1\1-AI_workflow\antigravity\reports\*.md`；协作板 `...\antigravity\COLLAB.md`（任务队列 + 回执）。
