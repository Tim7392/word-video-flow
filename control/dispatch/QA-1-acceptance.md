# 派工单 QA-1｜独立验收与坏样本（第一波）

- **基础 commit**：`966c683e738c1c24becdb61b0bb4b0c343eb9af4`（分支 `task/QA`）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\QA`
- **锁定解释器**：`D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe`
  自测时把 TEMP/TMP 指到 `D:\1\1-AI_workflow\word_video_flow\runtime\tmp\QA`（自己建）。
- **角色提示词**：`control/prompts/QA.md`（先读）

## 为团队解除什么阻塞

M0 已经证明了旧验收器有**会放过坏归档**的洞（删掉整条音标轨仍 PASS、集体平移 100ms 仍 PASS、
空目录 PASS、读到旧报告也 PASS）。这些反例现在只存在 M0 的证据目录里，没有成为新仓库的
回归资产。本任务把它们**正式化到新仓库**，让之后每一个集成 commit 都能自动发现同类问题。

## 可改范围（白名单）

- 新增：`tests/acceptance/**`（验收器、坏样本生成、跑批脚本）
- 新增：`tests/test_acceptance_*.py`（验收器自身的自测：坏样本必须被判 FAIL/REFUSED）
- **禁止改任何生产实现**（`word_video/**`、`word_video_cli.py`、根目录旧字幕工厂 6 文件、`legacy_tests/**`），
  QA 不能为了通过而改生产代码；也不改 `AGENTS.md`、`control/**`、`docs/**`、其他 worktree。

## 交付物

1. **移植并复核** M0 的验收资产（`D:\1\1-AI_workflow\word_video_flow\tools\m0\` 只读参考）：
   `accept_range.py`（八面：integrity/video/mix/srt/draft/timing/audio_e2e/pixels，含独立复算的
   节奏常量与词表解析、REFUSED 语义）、`accept_all.py`（多批、去陈旧报告）、
   `make_faults.py`（硬链接造坏样本）、`run_fault_matrix.py`（old/new 对照）。
   移植后要能在**新 worktree 内自洽运行**（路径不写死 M0 目录）。
2. **坏样本矩阵自测**：至少覆盖 M0 验证过的洞，并逐个断言"必须被判失败"：
   删整条音标轨、50 条音标统一 +100ms、时间线/SRT/草稿协同改词、空批次目录、
   缺批次、重复批次、陈旧报告（stale report）、损坏 `timeline.json`。
   期望值来自原词表 / 真实媒体 / 手算常量，**不得用生产 solve() 生成期望**。
3. **真跑一次**：用正常对照（`D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染\...\0151-0200`）
   跑全量验收并贴报告摘要；再跑坏样本矩阵并贴"每个坏样本的实际判定"。
   注意：该归档是**只读**，造坏样本必须硬链接到自己 worktree 外的 tmp 目录，
   且**绝不能写回原归档**（M0 曾发生过写穿硬链接的事故，必须复制 `complete.json` 而非链接）。
4. **W01 反例预备**（A 正在实现 `wv-project@1`）：先写好"期望必须失败"的用例清单
   （重复 ID、环、悬空、未知时长、过期 revision、教学重叠），等 A 的接口落地后接到
   `tests/test_wv_project_golden.py` 对应接口上；本轮不阻塞在 A 上。

## 报告格式（只报这四项）

改了什么（含 commit hash） → 测了什么及实际结果（命令 + 真实判定表） → 未验证什么 → 下一步/回滚 commit。

## 隔离事实

**只用绝对路径写文件**（子 Agent 默认 cwd 是 `D:\1\1-AI_workflow`，不是你的 worktree；
探针实测绝对路径可写、无需额外授权）。禁止写 `D:\1\1-AI_workflow` 下的任何位置，
旧归档只读。
