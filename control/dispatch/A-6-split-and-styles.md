# 派工单 A-6｜为 W06 解阻：拆分命令 + 工程级样式覆盖（小改，先做；W05 随后）

- **基础 commit**：合并后的 main（含 A-5、B 的 W07 flip、QA-3；H0 会给 hash）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\A`（`git checkout -b task/A6 main`）
- **角色提示词**：`control/prompts/A.md`
- **为什么插队**：C 正在做 W06 编辑器，两个验收点被卡住（拆分、"改字号"没有工程级入口）。
  这两件都是你的范围内的**小改**，做完 C 就能按验收走完；W05（协调器/CLI）随后你再开。

## 缺口 1：拆分片段需要一个应用命令

事实（C 报，H0 已核对）：`word_video/application/commands.py` 的 `COMMANDS` 只有
`MoveClip / MoveClips / TrimClip / SetMediaSlice / BindStart / UnbindStart`，**没有创建片段的命令**，
所以"把一段切成两段"无法经 `apply()` 完成；UI 自己造 Clip 是明令禁止的（没有 revision、没有撤销项、没有校验）。

最小接口（形态你定，但要满足）：
- `SplitClip(clip_id, at_ticks, new_clip_id='')`：左半保留原 id，右半用新 id；
- 链接片段拒绝（`CLIP_BOUND` 一类）；切口必须严格落在 `(start, end)` 内（否则 `INVALID_TIME`）；
- 有 source 的片段按**源网格**切窗口（保持你的 tick/有理数规则）；
- **明确适用范围并在实现里守住**：per-record 角色（female/male/chinese/english/phonetic/meaning）
  切两段会被 `compile._record_items` 判 `ROLE_AMBIGUOUS`；title/subtitle/footer 还会被
  B 的 `verify_manifest` 按窗口拒绝。所以拆分**只对工程图层（background/intro）有意义**——
  请让命令对不可拆的角色给出**结构化拒绝**（而不是让 C 做出一个"点了就报错"的按钮），
  并把这条限制写进 docstring 与测试。

## 缺口 2：工程级样式覆盖（"改字号"没有入口）

事实：`Project` schema 里没有 styles，`exporters/render.py::_styles()` 只读 `default_styles()`，
所以成员改字号只能改**模板默认值**（会改所有人）。而"改字号不得触发合成"是既有验收点，
说明字号本来就被当作可改的编辑对象。

要求：
- 在工程文档里加**每角色的样式覆盖**（字体/字号/颜色/位置，字段取现有 `default_styles()` 的键，
  缺失字段回落到默认；**不允许**在这里换字体族而静默替换——字体仍是既有解析链）；
- **版本化**：`wv-project@2` → 由你定（升版本或补丁口径），必须满足旧文档原样加载、行为不变；
  版本决定请写进报告，由 H0 转达 B/C；
- 计划/`LayoutSurface` 必须读到覆盖后的样式（B 的导出器与 C 的画布都从计划/样式解析，不能各读一份默认值）；
- 验收：改字号后**渲染出的文字实际变大**（给像素或布局判定，不要只看 JSON）、
  且**不产生任何 TTS 调用**（用假 provider 计数）。

## 可改范围

`word_video/domain/**`、`word_video/application/**`、`word_video/storage/**`、
`word_video/exporters/render.py`（只读样式解析那一处，最小改动；其余 exporters 归 B）、
新增 `tests/test_wv_split_clip.py`、`tests/test_wv_project_styles.py`

**禁止改**：`word_video/{media,voices,layout,importers}/**`、`preview/**`、`desktop/**`、`packaging/**`、
旧字幕核心、`AGENTS.md`、`control/**`、`docs/**`、其他 worktree、旧归档

## 验收（必须真跑并贴结果）

1. 拆分：background 段切成两段后 revision 前进、撤销可回到原状（逐对象断言 tick 不变）；
   对 per-record 角色与文字层给**结构化拒绝**（贴错误 code 与 object_path）。
2. 样式覆盖：改 `english` 字号 → 布局/像素证据显示文字更大；同一工程**不改字号**时产物与今天一致。
3. TTS 调用数 = 0（假 provider 计数断言）。
4. 旧 `@2`/`@1` 文档加载后 `to_dict()` 逐字不变、计划逐片段相同。
5. 全量：`python -m pytest tests`（默认口径，约 2-3 分钟）。

## 禁止事项

只用绝对路径写文件；不新增依赖；不改 B/C 的范围；不 force push、不 merge；不递归派子 Agent；
不降低验收阈值；不为了让 C 好写而在 UI 侧留"绕过 apply"的口子。

## 报告格式（只报这四项）

改了什么（含 commit hash 与**版本号决定**） → 测了什么及实际结果 → 未验证什么 → 下一步/回滚 commit。
