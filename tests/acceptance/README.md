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

它包含三件事，**全部写稳定证据**（不是 pytest tmp——tmp 会被 pytest 收尾删掉，证据被删就等于没有）：

| 步骤 | 测试 | 内容 | 实测耗时 |
|---|---|---|---|
| 媒体矩阵 | `tests/test_acceptance_fault_matrix.py`（18 项） | 好样本必须 PASS + 14 类坏批次/坏归档必须 FAIL 且命中指定的面 | 约 13 分钟 |
| 归档复现 | `tests/test_gate_reproduction.py`（1 项） | 用归档自己的 timeline 造请求（`provider.kind=local` 指未加工音频）重跑 50 词，与归档逐字段 / 五轨 SRT 逐字节 / 172 文件 sha256 对照 | 首次约 100 s；源码未变则复用 |
| 策略判定 | `tests/test_gate_policy.py`（1 项） | 同一输入出 v2 与 legacy 两批，分别用 `--cleaning v2 --spoken-file 我的表` 与 `--cleaning legacy` 判 PASS，并逐字段说明两批只差朗读文本 | 首次约 3 分钟；源码未变则复用 |

**证据路径**（固定，报告里请带这个路径）：

```
D:\1\1-AI_workflow\word_video_flow\out\reports\
    gate-matrix-<日期>.json              媒体矩阵汇总（15 行判定表 + 命中的面）
    gate-matrix-out-<日期>\range-*.json  每个样本的完整八面报告
    reproduction-vs-archive.json         复现批次 vs 归档的字段/SRT/哈希对照
    reproduction-request.json            本次复现用的请求（证据可复现）
    policy-accept-v2.json / policy-accept-legacy.json
    policy-legacy-vs-archive.json         legacy 批必须仍等于归档
    policy-v2-vs-legacy.json              两批差异面（只允许朗读文本 + 05 轨）
    stamp-*.json                          指纹：哪次运行的源码/输入与这份证据对应
```

`out\reports` 不可写时会**响亮退化**到 `runtime\tmp\QA\gate`，并在证据里记 `warning`；
`run_fault_matrix.py` 的 `--json` 一律给稳定路径，不要只留在 pytest tmp。

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
