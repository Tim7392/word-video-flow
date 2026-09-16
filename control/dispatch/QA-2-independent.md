# 派工单 QA-2｜独立复验：成员包 + 归档复现 + v2 策略 + W01 反例

- **基础 commit**：合并后的 main（含 A 的 W01、B 的 W02；H0 会给准确 hash）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\QA`（建议 `git checkout -b task/QA2 main`）
- **角色提示词**：`control/prompts/QA.md`（先读）
- **前置**：你的 QA-1 验收集已交付（H0 已收到）

## 为什么让你做这件事

现在有三样"看起来已经好了"的东西，但都是**生产者自己证明的**：
成员包（C 自验）、归档复现（H0 亲跑）、v2 清洗策略（A 正实现）。
独立验收的价值就在于：**换一套期望来源、换一条路径**再判一次。

## 任务

1. **成员包独立复验**（C 已重建到合并后引擎，路径与版本号问 H0 或看 `out\packages\`）
   - 你可以读 C 的 `packaging\verify_member_package.py` 作为**参考**，但**不要只跑它**：
     至少自己独立做两件事：① 在清洗环境（PATH 只留 `C:\Windows\System32`、清空 `PYTHONHOME/PYTHONPATH`、
     cwd 在包内）用**你自己的**三词请求跑一次，产出三产物并用**你自己的**判据核对
     （文件数、MP4 时长/分辨率、五轨 SRT 非空、草稿含独立文字与配音对象、`complete.json` 逐文件哈希）；
     ② 检查包里是否有密钥形状、真实素材、音频缓存、全量词表（自己扫，不复用它的扫描器结论）。
   - 记录：启动耗时、作业耗时、你实际用的环境变量与 PATH。
2. **归档复现的独立判据**（H0 已跑过一次，请用**你自己的**方法复判）
   - 用 `D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-50词复现-local.json`
     （provider.local，零网络）重跑到你的 tmp，然后与
     `D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染\wv-b349c24dbf19ce65bc4e5422\0151-0200`
     对照。H0 用 `tools\compare_batches.py` 得到 timeline 0 差异 + 五轨 SRT 全等；
     **请你用自己的比较脚本或手工抽样复核**（至少抽 10 个词逐帧核 + 全部五轨 SRT 逐字节核），
     并说明你的期望值来源。
3. **v2 朗读字段清洗策略的独立判定**：A 正在实现 `lesson.spoken_policy`（默认 v2）。
   等 A 交付后（H0 会通知你），用你的验收器（`--cleaning v2`）判 A 产出的批次，
   并用 `--cleaning legacy` 判同一输入的 legacy 批次；两次都要 PASS，否则报最小反例。
   **不要读 A 的 `text_policy.py` 来生成期望**（那样就不独立了），期望来自已批准的规则文本 + 原词表。
4. **W01 反例接线**：`tests/test_wv_project_counterexamples.py` 接到真实接口，按错误 `.code` 断言
   （`DUPLICATE_ID`/`DANGLING_REF`/`CYCLE`/`UNKNOWN_DURATION`/`SOURCE_RANGE`/`UNSUPPORTED_RATE`/
   `MISSING_ROLE`/`ROLE_AMBIGUOUS`/`TEACHING_ORDER`/`TEACHING_OVERLAP`/`STALE_REVISION`/`CLIP_BOUND`）。
   不要改 A 的 `tests/test_wv_project_golden.py`。

## 纪律

- 你不改生产实现；期望值来自原词表/真实媒体/手算常量。
- 造坏样本只在你的 tmp 里（复制或硬链接），**绝不能写回原归档**；`runtime\tmp\QA` 已占约 8.7 GB，
  需要更多空间先问 H0，别悄悄堆。
- 重测试独占本机槽位；不 force push、不 merge、不递归派子 Agent。
- 未验项如实写（例如"未在真正的第二台机器上验证"）。

## 报告格式（只报这四项）

改了什么（含 commit hash） → 测了什么及实际判定表（含你自己判据与生产者判据的差异） →
未验证什么 → 下一步/回滚 commit。
