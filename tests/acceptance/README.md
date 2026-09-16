# tests/acceptance — 独立验收器与坏样本（QA 所有）

M0 已经证明旧验收器会**放过坏归档**（删掉整条音标轨仍 PASS、集体平移 100ms 仍 PASS、
空目录 PASS、读到旧报告也 PASS）。这里把那批反例正式化进新仓库，让每次集成都自动复查。
所有期望值来自：**只读原词表**（`subtitle_factory_api.load_words` 的独立复现，见
`accept_range.load_expectations`）、**真实媒体**（ffprobe/ffmpeg 探测）和**手算常量**
（`tests/test_acceptance_selfcheck.py` 逐条断言）。**任何期望都不由生产 `solve()`/导出结果生成。**

## 门禁：一条命令

```powershell
# 在 worktree 内（默认套件已经 -m "not acceptance_media"，所以这里显式跑标记）
cd D:\1\1-AI_workflow\word_video_flow\worktrees\QA
& D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe -m pytest -m acceptance_media tests
```

它包含八件事，**全部写稳定证据**（不是 pytest tmp——tmp 会被 pytest 收尾删掉，证据被删就等于没有）：

| 步骤 | 测试 | 内容 | 实测耗时（单独跑） |
|---|---|---|---|
| 媒体矩阵 | `tests/test_acceptance_fault_matrix.py`（18 项） | 好样本必须 PASS + 14 类坏批次/坏归档必须 FAIL 且命中指定的面 | 约 13 分钟（空闲）/ 25 分钟（并行） |
| 归档复现 | `tests/test_gate_reproduction.py`（1 项） | 用归档自己的 timeline 造请求（`provider.kind=local` 指未加工音频）重跑 50 词，与归档逐字段 / 五轨 SRT 逐字节 / 172 文件 sha256 对照 | 首次约 100 s；源码未变则复用 |
| 策略判定 | `tests/test_gate_policy.py`（1 项） | 同一输入出 v2 与 legacy 两批，分别用 `--cleaning v2 --spoken-file 我的表` 与 `--cleaning legacy` 判 PASS，并逐字段说明两批只差朗读文本 | 首次约 6 分钟；源码未变则复用 |
| 采样数独立复算 | `tests/test_acceptance_audio_samples.py`（1 项） | 解码 AAC 后数样本，验证引擎的 `audio_sample_count` 与"包数×帧长"不是同一个数 | < 1 s |
| 多段背景 | `tests/test_acceptance_background_segments.py`（6 项） | 计划两段 / 像素证明两段都进片 / 草稿可编辑对象且源区间不越界 / 段间留洞拒绝 / 段间重叠拒绝 / 片头拆分拒绝 | 约 6 s |
| 归档扫描（B 提供） | `tests/test_wv_import_cache.py`（2 项） | 全归档每条记录与它指向的文件一致 | 约 140 s |
| 协调器端到端（A 提供） | `tests/test_wv_coordinator_e2e.py`（1 项） | 真实批次走协调器并发布 | 约 4 s |
| **发布目录** | `tests/test_gate_published.py`（1 项） | **两个工程**：① 带 `delivery.json` → 不传 `--background` 也 `ready=true`，且计划继承的那条与文件一致；② 无交付设置 → `ready=false` + `BACKGROUND_MISSING`、submit `NEEDS_INPUT` 且不留收据；再有背景 submit→run，在 `runs\<job>` 上判三份文档路径、无 staging、`timing.complete=true` | 约 38 s |
| **导入闭环** | `tests/test_gate_import_voices.py`（1 项） | 没有音色的工程 → `import audio --roots <cache> --voices … --apply` 真把 voice 写进 `assets.json`、再导一次字节不变、`plan` 里那条 fix 消失且 `ready=true`、随后能 submit | 约 6 s |
| **交付继承（headless）** | `tests/test_gate_delivery_inheritance.py`（1 项） | **真子进程**跑 `python -m word_video.exporters`：① 带 `delivery.json` 且不给 `--background` → exit 0，且草稿 `word_video_manifest.json` 的 `background`/`video_codec` 等于工程里那条；② 无 `delivery.json` 且**保留草稿**（只加 `--no-video`）→ exit 2、stdout 单个 JSON、`code=BACKGROUND_MISSING`、`object_path=profile.background`、`fixes` 非空、**out/run 目录没建**、cwd 条目不变、无 `PermissionError`；并记录 `stage_seconds` | 约 10 s |

**门禁共 33 项**（另有 `test_wv_media_streams.py` 1 项由 B 新加，同属该标记）。
实测一次完整门禁（**不含**发布目录/导入闭环/交付继承三步）：**30 passed / 墙钟 2056 s（约 34 分钟）**，
但那次是**与 A、B、C 三个 Agent 并行**跑的（16 逻辑核，负载 71%），媒体矩阵被拖到 25 分钟；
同一台机器空闲时媒体矩阵 13 分钟。所以**独占槽位预期 20–21 分钟，拥挤时 34 分钟是上界**——
报这个数字时请连负载一起报。默认套件不受影响（`pytest.ini` 是 `-m "not acceptance_media"`）。

### 交付语义：计划继承工程交付设置，CLI 参数覆盖

`batch plan` / `batch submit` 的交付档位**先取工程自带的 `delivery.json`**（编辑器写的那份），
`--background` 只是**覆盖**它。所以：

- **带** `delivery.json` 的工程，不传 `--background` 也是 `ready=true`，且计划里的 background
  就是文件里那条（这一步会拿两者比对，不是只看 `ready` 标志）；
- **判拒绝面必须用没有交付设置的工程**（本步骤用 `make_real_project.py --no-delivery` 造一个），
  拿带背景的工程去判"缺背景"会得到 `ready=true`——那不是缺陷，是设计。

### 跑门禁前必须知道的四件事（否则容易误判成门禁坏了）

1. **`assets.json` 里每个语音资产必须有 `voice`。** 缺音色记录时 `batch submit` 会（正确地）
   返回 `NEEDS_INPUT`。**推荐用文档路径准备它**：`import audio --roots <旧缓存根> --voices female=…,male=…,chinese=… --apply`（A-8 起真的落盘且幂等），
   `tests/test_gate_import_voices.py` 就是这条闭环的看门测试；发布目录那一步为了不依赖导入命令，
   仍然**直接写 `assets.json` 的 `voice` 字段**（也算文档允许的方式），报告里会注明用的是哪条路径。
2. **`timeline.json` 允许引用运行目录之外的文件**（本次交付的背景 + 已安装字体）；
   `complete.json` 与草稿必须全部落在运行目录内。口径不同是刻意的。
3. **判断拒绝面要看收据增量，不要看 `receipts` 是否为空**：全局收据本来就含此前成功那次的记录，
   判据是"拒绝前后条数不变 + 没有该 idempotency_key 的收据"。
4. **发布目录那一步需要两个工程**（见上一节）；只造一个带 `delivery.json` 的工程会让"拒绝面"
   必然为假。

**证据路径**（固定，报告里请带这个路径）：

```
D:\1\1-AI_workflow\word_video_flow\out\reports\
    gate-matrix-<日期>.json              媒体矩阵汇总（15 行判定表 + 命中的面 + _evidence 路径）
    gate-matrix-out-<日期>\range-*.json  每个样本的完整八面报告
    reproduction-vs-archive.json         复现批次 vs 归档的字段/SRT/哈希对照
    reproduction-request.json            本次复现用的请求（证据可复现）
    reproduction-batch.txt               本次复现批次目录（复用时指向上次的批次）
    policy-accept-v2.json / policy-accept-legacy.json
    policy-legacy-vs-archive.json         legacy 批必须仍等于归档
    policy-v2-vs-legacy.json              两批差异面（只允许朗读文本 + 05 轨）
    gate-aac-samples.json                 采样数三种口径的复算
    gate-published-<日期>.json            发布目录判定（继承 / 拒绝面 / 三份文档 / 运行状态）
    gate-published-accept-<日期>.json     在该发布目录上的八面判定
    gate-import-<日期>.json               导入闭环（voice 落盘 / 幂等 / blocker 消失）
    gate-delivery-<日期>.json              headless 交付继承与拒绝面（+ stage_seconds）
    stamp-*.json                          指纹：哪次运行的源码/输入与这份证据对应
```

`out\reports` 不可写时会**响亮退化**到 `runtime\tmp\QA\gate`，并在证据里记 `warning`；
**所有** 步骤（含媒体矩阵）的 `--json` 都写这里，不允许只留在 pytest tmp——tmp 会被 pytest
收尾删掉，等于没有证据。

## 复用规则（为什么第二次跑很快）

每个重步骤在 `out\reports\stamp-<步骤>.json` 里记指纹：**引擎源码 + 验收工具 + 只读词表**的
sha256。指纹一致 → 上一步的成片与判定仍然有效，测试直接复用并在 stamp 里写 `reused: true`
与上次的 commit；**任何新 commit 都会自动让指纹失效并重跑**，不需要人记得清缓存。
判定（`accept_range` / `compare_to_archive` / `policy_check`）本身很便宜，每次都重新跑，
即使成片是复用的。

## 文件

| 文件 | 作用 |
|---|---|
| `accept_range.py` | 判一个批次，八面：integrity / video / mix / srt / draft / timing / audio_e2e / pixels；缺独立期望时判 `REFUSED`（退出码 2），永不判 PASS；`--spoken-file` 用外部推导的朗读文本表判定并记录来源 |
| `accept_all.py` | 判一个归档：`--expect` 声明的范围必须齐全、唯一、可读；陈旧报告不得代表本次运行 |
| `make_faults.py` | 从一个已交付好批次派生坏样本（NTFS 硬链接，`complete.json` **复制**不链接） |
| `run_fault_matrix.py` | 把每个坏样本喂给上面的验收器，输出"期望 vs 实际"判定表 |
| `gate.py` | 门禁的公共部分：稳定证据目录（含响亮退化）、指纹与复用判定 |

上游工具：`tests/qa2/build_request.py`（从归档造请求 / source+range 模式）、
`tests/qa2/compare_to_archive.py`（与归档对照）、`tests/qa2/package_run.py`（成员包受限期运行）、
`tests/qa2/scan_package.py`（包内敏感物扫描）、`tests/qa3/derive_spoken.py`（按已批准规则推导
legacy/v2 朗读文本）、`tests/qa3/policy_check.py`（两批差异面判定）。

## 手动跑法（在 worktree 内自洽，不依赖 M0 目录）

```powershell
$py  = 'D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe'
$wf  = 'D:\1\1-AI_workflow\word_video_flow\worktrees\QA'
$tmp = 'D:\1\1-AI_workflow\word_video_flow\runtime\tmp\QA'      # 可写；归档只读
$out = 'D:\1\1-AI_workflow\word_video_flow\out\reports'         # 稳定证据
$good = 'D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染\wv-b349c24dbf19ce65bc4e5422\0151-0200'
$wl  = 'D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt'
$env:TEMP = $tmp; $env:TMP = $tmp       # 真实媒体验收占本机槽位：同一时间只跑一个

# 1) 正常对照：必须 PASS
& $py "$wf\tests\acceptance\accept_range.py" $good --source $wl --range 151-200 `
      --cleaning legacy --pixels all --json "$out\archive-legacy-pixels.json"

# 2) 造坏样本（硬链接，只读归档绝不被写）
& $py "$wf\tests\acceptance\make_faults.py" build --good-batch $good `
      --faults "$tmp\faults" --wordlist $wl --range 151-200 --verify-original

# 3) 坏样本矩阵：每个坏样本的期望与实际判定
& $py "$wf\tests\acceptance\run_fault_matrix.py" --good-batch $good `
      --faults "$tmp\faults" --wordlist $wl --out "$out\matrix-out" `
      --range 151-200 --pixels all --json "$out\gate-matrix-manual.json"

# 4) 快速自测
& $py -m pytest -q tests\test_acceptance_selfcheck.py tests\test_wv_project_counterexamples.py
```

## 纪律（不要退化）

- 归档**只读**：坏样本只能硬链接到 `runtime\tmp\QA` 下，`complete.json` 必须复制；
  `make_faults.py --verify-original` 会重算原归档每个文件的 sha256 作为硬证据。
- **不降阈值、不删断言、不用 SKIP 或旧 PASS 顶替**：缺媒体/缺词表时相关测试**失败**并写明路径，
  不会变绿；`accept_range` 没有独立期望时判 `REFUSED`。
- 坏样本必须声明"哪个面必须先抓到它"，只判 `FAIL` 不算通过（避免因为别的原因失败而冒充证据）。
- **证据只认稳定路径**：`--json` 必须写到 `out\reports\`（或明确记录的退化目录），
  报告里带上路径；只落在 pytest tmp 的运行等于没有证据。
- 真实媒体验收占本机槽位，同一时间只跑 1 个全量验收/渲染。
