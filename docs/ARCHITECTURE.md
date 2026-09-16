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

## 本仓库当前代码形态（迁移起点，不是重写目标）

```
repo/
  word_video/            # 现引擎：contracts timing tts original_tts jianying_tts jobs draft
                         #           template media srt_export voices validate reference
                         #           render/{__init__,ass,mix}
  legacy_subtitle 6 文件 + legacy_tests/  # 平铺在仓库根目录（逐字复制未改，见 docs/LEGACY_SUBTITLE.md）
  word_video_cli.py      # 现 headless JSON CLI（14 个 action，stdout 单 JSON）
  word_video_package.py  # 打包入口
  tests/                 # 19 个测试文件；默认离线（conftest 阻断非回环连接）
  docs/ control/         # 本文档与任务清单/角色提示词
```

演进方式：在 `word_video/` 内按 A/B 的模块边界生长 domain/application/storage/media/voices/
layout/exporters 子包，**不做推倒重写**；现有 `timeline.json` 生产链是可复用资产，
新工程模型先与它共存（W01 先交最小接口与手算夹具，W02 消费它）。

## 公共接口变更流程

只有公共 schema/CLI 契约变化才更新本文件：由 A 单写并提升接口版本，H0 合并后通知 B/C/QA
使用同一版本；变更需附相关测试与最小反例。
