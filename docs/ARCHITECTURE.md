# 架构说明（R1.1 现场版）

本文件只记录**不变项**与权威来源。完整顶层设计以原始文档为准，本仓库不改写它。

## 权威来源（保持原样，不在本仓库重写）

| 文档 | 位置 | 作用 |
|---|---|---|
| 视频制作软件_顶层架构_v1 | `docs/r1/视频制作软件_顶层架构_v1.md`（原件：`D:\1\1-AI_workflow\视频制作软件_顶层架构_v1.md`） | 顶层架构基线 |
| R1.1 阶段与子 Agent 并行任务 | `docs/r1/03_阶段与子Agent并行任务.md`（原件：WORK_ROOT 根目录） | 五阶段/十二任务/并行规则 |
| R1.1 原则补丁（包内模板） | `docs/r1/AGENTS_包内模板.md` | 原则原文；现场值已在 `AGENTS.md` 填实 |
| 产品约定（第二阶段） | `D:\1\1-AI_workflow\单词视频工坊_第二阶段产品约定_20260916.md` | 产品边界，未迁入（原件权威） |

## 不变项（改这些需要顶层裁决）

1. **模块化单体**：一套纯 domain + 一套 application 命令，UI/CLI 使用同一业务。
   新应用 PySide6；旧 PyQt 字幕工厂独立进程。Python 做控制，
   FFmpeg/PyAV/Qt 原生实现做媒体重活，不写 Python 逐像素循环。
2. **ProjectDocument 是唯一可编辑真相**；模板只在创建/显式升级时展开；
   RenderPlan/CuePlan 只读派生；720000 tick/s 及源有理数时基；
   配音完整加工到结束后按实际样本数定时；不截尾、不重复变速。
3. **本机一个按需协调器**负责写入、版本冲突、任务和资源；没有任务就不占用重型 worker；
   不把每个模块做服务。SQLite 只存任务/索引/收据，不另存一份可编辑工程。
4. **任务提交固定工程/输入意图**，媒体就绪再固定实际音频/计划；
   同一素材入库一次后以资产 id 引用，批次不反复复制；
   新任务不读取编辑中的工程或仍可变的外部路径。
5. **连续预览**首选源素材低清代理/按需解码 + 独立文字层 + 统一 PCM 时钟；
   缓存有界、版本和 seek 代际明确。首次验证不搭建通用共享内存总线，
   不以多播放器各播一轨代替同步。主路线核心指标实测不达标时提交最小反例和替代方案，
   先找顶层裁决，不并行养两套媒体引擎。
6. **预览与 MP4 共用文字排版/计划**；剪映单向导出，保留独立可编辑对象；
   MP4 由自有渲染完成，不依赖剪映 GUI 才能批量出片。
7. **Agent 自动化是首版能力**：稳定本地 CLI（capabilities、doctor、project/voice/template、
   batch plan/submit、job status/cancel/resume、artifacts）；stdout 单个 JSON，日志 stderr，
   watch 用 NDJSON；结构化错误；non-interactive 默认 `NEEDS_INPUT`；
   `plan` 只预检/估算，`submit` 需 idempotency key + 固定计划/工程版本 + 有效 TTS 权限；
   同键同请求返回原任务，不同内容冲突。先完成 CLI，不另造云 API/MCP 平台。
8. **旧线保护**：旧字幕核心、旧入口、旧归档只读；新入口通过独立子进程适配（W10）。

## 当前代码形态与公共接口（截至 2026-09-16，主干 `9c3dc11` 一带）

```
repo/
  word_video/
    domain/          # 唯一可编辑真相与只读派生：model(timebase/Project/Clip/MediaSlice)
                     #   plan(RenderPlan/CuePlan) compile solve rhythm lesson errors
    application/     # 命令层（UI/CLI 共用）：commands(Move/Trim/SetMediaSlice/Bind…)
                     #   instantiate(模板展开) session(撤销/模式) intro(片头测量与音源裁决接线)
    storage/         # project_store(原子写/严格读) assets(wv-assets@1 资产登记与解析)
    cli/             # 新命令入口（W05 落成中）
    media/           # 媒体链：core/intro/streams/proxy/audio（片头音源唯一裁决、真实 PTS/样本、代理、重叠混音）
    voices/ layout/ exporters/   # 音色定义；LayoutSurface（预览与成片唯一排版）；三产物导出
    importers/       # 旧缓存导入（cache）、旧剪映配音/音色导入（jianying/store）
    text_policy.py   # 朗读文本清洗（默认 v2，legacy 供复现历史）
    tts.py timing.py jobs.py draft.py render/ template.py srt_export.py …（旧链，保持可用）
  preview/ desktop/  # W04 连续预览引擎（Qt-free）+ 独立画布/音频输出
  packaging/         # 成员目录式包（PyInstaller onedir + 包内 ffmpeg + 启动器 + 清洗环境验收）
  legacy_subtitle 6 文件 + legacy_tests/   # 平铺在根目录（逐字复制未改，见 docs/LEGACY_SUBTITLE.md）
  word_video_cli.py  # 旧 headless JSON CLI（submit/start/…；新命令由 W05 增加）
  tests/ tests/acceptance/ tests/qa2/ tests/qa3/   # 回归 + 独立验收（重验收用 -m acceptance_media）
  tools/             # H0 核对工具（保护核对、批次比较、口径核对等）
```

### 已冻结的公共契约（消费者必须用同一版本）

| 契约 | 版本/入口 | 说明 |
|---|---|---|
| 工程文档 | **`wv-project@3`** | `@1`/`@2` 原样加载、行为不变；片头是工程里的一层（`intro`），时长**来自媒体**；每角色样式覆盖（`SetStyle/ClearStyle`，**字体不可在工程层替换**）；`SplitClip` 只对 `background` 生效（片头/文字/词角色在**命令层**拒绝） |
| 资产登记 | **`wv-assets@1`** | `<工程目录>/assets.json`；`resolve()` 出 `asset_id → 路径 + MediaInfo`；错误码 `MISSING_ASSET`/`DUPLICATE_ASSET`/`ASSET_FILE_MISSING`/`UNKNOWN_DURATION`；可选 `voice` 记录字段；无登记表时「id 即路径」兜底 |
| 音色登记 | **`wv-voices@1`** | 导入的旧配音/音色，带 `status`（仅可复用/可合成/待验证/需剪映操作） |
| 协调器 | **`wv-coordinator@1`** | 一个工作根一个写者；提交即冻结（收据 = 计划身份+导出配置+修订+输入 sha256）；`staging → 校验 → os.replace → 同事务登记产物`；取消入 `recovery/`；`reconcile` 判 `interrupted`/`partial_failed`（**永不把半成品当成功**）；SQLite 只存 `projects/jobs/receipts/artifacts` |
| CLI（Agent 入口） | **`wv-cli@1`**，`python -m word_video.cli` | `capabilities/doctor/project/batch plan\|submit/job status\|cancel\|resume\|run/artifacts/receipts/reconcile/watch`；stdout 恒一个 JSON、`watch` 用 NDJSON、缺信息 `NEEDS_INPUT+fixes`、退出码 0/2/1；`batch plan` 不落文件；`voice`/`template` 动作待补 |
| 排版 | `word_video.layout.LayoutSurface` | 预览与 MP4**唯一**排版；坐标 0..1、字号按 `height/2160` 缩放、溢出是数据不是异常；**消费必须经 `application/styles.py::merged_styles`**（否则会静默丢掉未覆盖角色） |
| 三产物入口 | `python -m word_video.exporters` | `project.json` + 目录 → MP4 / 五轨 SRT / 可编辑剪映草稿；**只读计划**，不依赖剪映 GUI；多段背景按计划 item 顺序落段，**多段片头明确拒绝** |
| 朗读文本策略 | `word_video.text_policy` | 默认 `v2`（去词性标记与其连接符、去内嵌音标）；`legacy` 仅用于复现历史批次 |
| 编辑器 | `python -m desktop`（窗口 + `--import/--open/--export` 脚本化驱动） | 编辑只经 application 命令；画布复用 W04 预览与 `LayoutSurface`；工程文件夹含 `project.json`/`assets.json`/`media.json`（后者在 `voice` 迁移完成后删）/`delivery.json`/`.preview`（代理缓存，跨重启保留） |
| 成员包 | `out\packages\word-video-member-<ver>` | 双 onedir（CLI console + 编辑器 windowed）+ 包内 ffmpeg + 启动器（无参数进编辑器）+ 清洗环境验收脚本 |
| 旧线 | `word_video_cli.py`、旧字幕工厂 | 保持可用；旧核心 6 文件逐字节未改，W10 只做子进程适配 |

演进方式：**不做推倒重写**——旧 `timeline.json` 生产链与新工程模型共存，
新入口的产物与旧链产物**过同一个独立验收器**（已验证：同一请求下五轨 SRT 逐字节相同、
归档可逐帧复现）。

## 公共接口变更流程

只有公共 schema/CLI 契约变化才更新本文件：由 A 单写并提升接口版本，H0 合并后通知 B/C/QA
使用同一版本；变更需附相关测试与最小反例。
