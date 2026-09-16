# STATUS

一行一条，最新在下。格式：日期 | 任务 | 状态 | 证据 | 未验/下一步。

## 2026-09-16

| 项 | 内容 |
|---|---|
| W00 工作根与基座 | **完成（本地）** | WORK_ROOT = `D:\1\1-AI_workflow\word_video_flow`；建立 `repo/ worktrees/ tools/ data/ runtime/ logs/ out/`；源码复制迁入（`word_video/` 16 文件、`tests/` 19 文件、CLI/打包/依赖清单、`legacy_subtitle/` 6 文件、`tools/m0/` 367 文件）；Git 本地基线建立（此前本机**没有任何** Git 版本管理：`PythonSRT` 无 `.git`，工作区 `.git` 是空壳） | 原树/旧归档/旧草稿未改；GitHub 未绑定，未 push |
| 三项待决 | 2 项补齐 | ① WORK_ROOT 已绑定（Tim 选定）；② TTS 单位 = **20000 次请求**，且 Q03 为 Q00 后续授权，允许真实调用；③ GitHub owner/repo **仍未提供** → 只本地 commit | 720p30/540p30 预览档位仍未批准冻结，实测后再请 Tim 确认 |
| R1.1 原则合并 | **完成** | 合并范围：`AGENTS.md`（现场路径/命令/边界/底线）、`docs/DIVISION.md`（分工与并行规则）、`docs/ARCHITECTURE.md`（架构不变项 + 权威来源）、`control/tasks.json`（W00–W11 静态清单）、`control/prompts/{H0,A,B,QA}.md`（角色提示词，含核心原则与禁止事项）；包内模板字段"实际工作根/测试命令/目标仓库"已填实 | 本轮**无活跃子 Agent**（尚未派工），故"增量同步"不适用；新任务直接使用更新后的角色提示词 |
| 环境 | 完成 | `runtime\venv` 锁定解释器 + `requirements-word-video.txt`（+pytest/pillow）；FFmpeg/FFprobe 走开发机既有 `C:\Users\Administrator\.local\bin` | 成员机不装 Python/FFmpeg 由候选包解决（W09） |
| 未验项 | 如实记录 | 未做真实剪映人工编辑；未在目标核显机验证；未做真实 TTS 调用（授权已给，等首批真实三词/50词任务）；未做跨机验证；无第二台电脑 |

## 下一步（第一波）

1. H0：子 Agent worktree 写入隔离探针（两轻量任务），确认隔离后并行，否则单写者+只读并行。
2. A=W01 唯一工程与编辑命令；B=W02 旧视频最小修复与媒体链；QA=坏样本验收集。
3. H0 同时准备三词真实预览素材（1080p 背景 + 透明片头 + 已有音频）。
