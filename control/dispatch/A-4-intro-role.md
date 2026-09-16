# 派工单 A-4｜把片头纳入工程模型（消除"导出参数 vs 工程真相"的临时分歧）

- **基础 commit**：合并后的 main（含 W01/W02/W03/v2/QA 验收集；H0 会给 hash）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\A`（`git checkout -b task/A4 main`）
- **角色提示词**：`control/prompts/A.md`
- **前置**：W01（`wv-project@1`）、A-2（`text_policy`）已并入 main

## 为团队解除什么阻塞

`ProjectDocument` 被定为唯一可编辑真相，但现在**片头没法在工程里表达**：
`CLIP_ROLES` 里没有片头素材角色，`Project` 只能给一个 `intro_s`（秒）。结果是
"含真实透明片头的课"必须靠**导出入参**才能出片——同一份 `project.json` 换个参数就能出不同的片子，
成员也无法在工程里看见/选中/静音片头。这违反"三产物同计划"和"新任务不读取仍可变的外部路径"。
B 本轮已用导出参数接住（H0 批准，保证 P1 当天打通），现在由你把它收回工程。

## 可改范围（白名单）

- `word_video/domain/**`（模型/计划/编译/节奏）、`word_video/application/**`（模板展开、命令）
- 新增测试：`tests/test_wv_project_intro.py`
- **禁止改**：`word_video/{media,voices,layout,exporters}/**`（B 的范围，你只提供接口）、
  `desktop|preview|packaging/**`（C）、`word_video_cli.py` 的既有 action 契约（可加**新** action 需先问 H0）、
  旧字幕核心、`AGENTS.md`、`control/**`、`docs/**`、其他 worktree、旧归档

## 要求

1. **片头成为工程里的一个 layer/clip 角色**（例如 `intro`），可带：视频素材（含透明通道）、
   可选外置音频、时长策略。**音源裁决只允许有一个实现**：直接复用 B 的
   `word_video/media/resolve_intro_audio`（片头自带音轨优先 → 否则 `intro_audio` → 静音片段按画面长度占位），
   **不要另写一份"片头用哪条音轨"的逻辑**。
2. **时长来自媒体本身，不是秒数常量**：现有 `intro_s` 保留兼容（旧工程仍可加载），
   但优先级要写清（建议：有片头素材时以媒体时长为准，`intro_s` 只作为无声/无素材时的兜底，
   并在报告里说明你选的口径与理由）。**不允许**出现"模板说 2 秒、素材 1.867 秒"这种自相矛盾还能出片。
3. **版本化**：要不要提升 `wv-project@1` → 版本号由你定，但必须满足：
   旧 `project.json` 仍能加载（缺 intro 时行为与今天一致）；新版本号写进报告，由 H0 转达 B/C 消费。
4. **计划必须承载片头**：`RenderPlan`/`CuePlan` 里能读出片头阶段（素材、时长帧、音源裁决结果），
   B 的导出器随后改成**只读计划**（他会拿到你的版本号）。
5. **模板**：模板里可以包含片头层；仍只在创建/显式升级时展开（不要每次求解都重算模板）。

## 验收（必须真跑并贴结果）

1. 旧工程（无 intro）加载后行为与今天一致（给一个前后对比或既有测试全绿）。
2. 含片头的工程：计划里片头阶段存在且时长来自媒体；静音片头片段与"只有外置音频"两种情况都正确。
3. 素材缺失/不可用时给**结构化错误**（有 code、指向哪个对象），不是崩溃或静默跳过。
4. 教学交付的严格规则（缺角色/顺序/重叠）不受影响：原有反例测试全绿。
5. 与 B 的 `resolve_intro_audio` 一致性：至少一个用例证明"你的计划里的片头音源"与
   `resolve_intro_audio` 的裁决相同（不要各自判断）。
6. 全量回归：`python -m pytest tests`（默认套件应保持约 1 分钟内；重验收用 `-m acceptance_media`）。

## 禁止事项

只用绝对路径写文件；不新增依赖（先问 H0）；不改 B/C 的范围文件（需要接口变更就报 H0 转达）；
不 force push、不 merge、不切分支；不递归派子 Agent；不降低验收阈值。

## 报告格式（只报这四项）

改了什么（含 commit hash 与**版本号决定**） → 测了什么及实际结果 → 未验证什么 → 下一步/回滚 commit。
