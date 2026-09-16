# tests/acceptance — 独立验收器与坏样本（QA 所有）

M0 已经证明旧验收器会**放过坏归档**（删掉整条音标轨仍 PASS、集体平移 100ms 仍 PASS、
空目录 PASS、读到旧报告也 PASS）。这里把那批反例正式化进新仓库，让每次集成都自动复查。
所有期望值来自：**只读原词表**（`subtitle_factory_api.load_words` 的独立复现，见
`accept_range.load_expectations`）、**真实媒体**（ffprobe/ffmpeg 探测）和**手算常量**
（`tests/test_acceptance_selfcheck.py` 逐条断言）。**任何期望都不由生产 `solve()`/导出结果生成。**

## 文件

| 文件 | 作用 |
|---|---|
| `accept_range.py` | 判一个批次，八面：integrity / video / mix / srt / draft / timing / audio_e2e / pixels；缺独立期望时判 `REFUSED`（退出码 2），永不判 PASS |
| `accept_all.py` | 判一个归档：`--expect` 声明的范围必须齐全、唯一、可读；陈旧报告不得代表本次运行 |
| `make_faults.py` | 从一个已交付好批次派生坏样本（NTFS 硬链接，`complete.json` **复制**不链接） |
| `run_fault_matrix.py` | 把每个坏样本喂给上面的验收器，输出"期望 vs 实际"判定表 |
| `w01_counterexamples.py` | W01 `wv-project@1` 的"期望必须失败"用例清单（接口落地前先写好） |

## 跑法（在 worktree 内自洽，不依赖 M0 目录）

```powershell
$py  = 'D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe'
$wf  = 'D:\1\1-AI_workflow\word_video_flow\worktrees\QA'
$tmp = 'D:\1\1-AI_workflow\word_video_flow\runtime\tmp\QA'      # 可写；归档只读
$good = 'D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染\wv-b349c24dbf19ce65bc4e5422\0151-0200'
$wl  = 'D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt'
$env:TEMP = $tmp; $env:TMP = $tmp       # 真实媒体验收占本机槽位：同一时间只跑一个

# 1) 正常对照：必须 PASS
& $py "$wf\tests\acceptance\accept_range.py" $good --source $wl --range 151-200 `
      --cleaning legacy --pixels all --json "$tmp\good.json"

# 2) 造坏样本（硬链接，只读归档绝不被写）
& $py "$wf\tests\acceptance\make_faults.py" build --good-batch $good `
      --faults "$tmp\faults" --wordlist $wl --range 151-200 --verify-original

# 3) 坏样本矩阵：每个坏样本的期望与实际判定
& $py "$wf\tests\acceptance\run_fault_matrix.py" --good-batch $good `
      --faults "$tmp\faults" --wordlist $wl --out "$tmp\matrix-out" `
      --range 151-200 --pixels all --json "$tmp\matrix.json"

# 4) 自测
& $py -m pytest -q tests\test_acceptance_selfcheck.py tests\test_wv_project_counterexamples.py
& $py -m pytest -q -m acceptance_media tests\test_acceptance_fault_matrix.py   # 重：需真实归档
```

## 纪律（不要退化）

- 归档**只读**：坏样本只能硬链接到 `runtime\tmp\QA` 下，`complete.json` 必须复制；
  `make_faults.py --verify-original` 会重算原归档每个文件的 sha256 作为硬证据。
- **不降阈值、不删断言、不用 SKIP 或旧 PASS 顶替**：缺媒体/缺词表时相关测试**失败**并写明路径，
  不会变绿；`accept_range` 没有独立期望时判 `REFUSED`。
- 坏样本必须声明"哪个面必须先抓到它"，只判 `FAIL` 不算通过（避免因为别的原因失败而冒充证据）。
- 真实媒体验收占本机槽位，同一时间只跑 1 个全量验收/渲染。
