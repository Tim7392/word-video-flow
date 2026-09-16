# 派工单 B-4｜软件硬化（生产已暂停，本单不含真实 TTS）

- **基础 commit**：合并后的 main（H0 会给 hash）；`git checkout -b task/B4 main`
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\B`
- **角色提示词**：`control/prompts/B.md`
- **前置**：W02/W03/W07 已合并

## Tim 的决定（现场规则）

**生产暂停**：不再开新批次消耗 TTS 额度，火力转到软件。**零网络的本地验证跑不算生产**，
本单全部在 `provider.kind=local` / 坏样本 / 对照范围内完成，**不得调用真实 TTS**。

## 任务（按顺序，做完一项报一项也行）

### 1. 50 词整批导入 + 与归档长跑对照（零网络）
- 用你的旧缓存导入，把 **151-200 的 150 条原件**导成一次真实 50 词批次（1080p/h265），
  产物交 `tools\m0\accept_range.py --cleaning legacy --pixels all` 判定 **PASS**；
- 与归档 `…\范围151-200-切片渲染\wv-b349c24dbf19ce65bc4e5422\0151-0200` 对照：
  timeline 逐字段、五轨 SRT 逐字节、`complete.json` 172 条 sha256；
- 证明**TTS 调用数 = 0**（你上轮的断网子进程 + 计数假 route 双重锁定继续用）；
- 记录整批耗时与各阶段耗时（这是"导入后能直接出片"的批量证据）。

### 2. `prepared_speech` 报错可诊断化（H0 踩过的坑）
现状：喂未变速原件时 `draft.py` 只报 `Speech exceeds allocated timeline stage`，看不出是哪个词/哪条音频/差多少。
要求：报错带**词序号、角色、涉及文件、计划秒数 vs 实际秒数**，并给出可执行提示
（"这份音频没有按 1.25× 加工过；复用已有音频请走 provider.kind=local"）。
留下反例测试（喂原件 → 断言错误信息包含上述字段）。

### 3. 为 A-6 的样式覆盖预备（等 A 合并后再验，不要抢跑改 A 的文件）
A 正在加"每角色样式覆盖"。落地后请确认样式**真的到达** manifest / ASS / 剪映草稿
（不是只存在于工程）：给一个"改 english 字号 → 三产物里字号确实变了"的实测（像素或 ASS 文本级）。
若发现 exporters 侧需要改动，那部分归你；A 只动 `render.py` 的样式解析一处。

## 可改范围

`word_video/{media,voices,layout,exporters,importers}/**`、`word_video/render/**`、
新增 `tests/test_wv_import_*.py` / `tests/test_wv_export_*.py`

**禁止改**：`word_video/{domain,application,storage,cli}/**`（A）、`preview/**`、`desktop/**`、
`packaging/**`（C）、旧字幕核心、`AGENTS.md`、`control/**`、`docs/**`、其他 worktree、旧归档

## 禁止事项

**不得调用真实 TTS**（生产已暂停）；只用绝对路径写文件；不改 A/C 范围；不新增依赖；
不 force push、不 merge；不递归派子 Agent；重编码/长测试独占本机槽位。

## 报告格式（只报这四项）

改了什么（含 commit hash） → 测了什么及实际结果（命令 + 真实数字与判定） →
未验证什么 → 下一步/回滚 commit。
