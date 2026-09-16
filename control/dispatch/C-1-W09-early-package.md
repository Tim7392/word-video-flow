# 派工单 C-1｜W09 早期最小目录式包（第一波，第三开发位）

- **基础 commit**：`829380e`（分支 `task/C`；H0 可能只追加文档提交）
- **绝对 worktree**：`D:\1\1-AI_workflow\word_video_flow\worktrees\C`
- **锁定解释器**：`D:\1\1-AI_workflow\word_video_flow\runtime\venv\Scripts\python.exe`
  （H0 已装 `pyinstaller 6.22.3`，打包脚本从这里跑）
- **角色提示词**：`control/prompts/C.md`（先读）

## 为团队解除什么阻塞

现在整条生产链**只有开发机能跑**：要有 Python 3.14、要有 venv 依赖、要有 PATH 上的
FFmpeg、还要知道 `word_video_cli.py` 在哪。成员按 R1.1 的要求不能装 Python/FFmpeg、
不能配 PATH。所以在做 UI 之前，先把"**一个目录拷过去、双击就出片**"这件事做出来：
这是 P1 验收里的"无开发环境依赖的单机候选包"，也是后面所有交付的地基。

## 可改范围（白名单）

- 新增：`packaging/**`（打包脚本、启动器模板、成员使用说明、版本文件）
- 新增测试：`tests/test_packaging_*.py`
- 产出目录：`D:\1\1-AI_workflow\word_video_flow\out\packages\**`（不在仓库内，不进 Git）
- **禁止改**：`word_video/**`、`word_video_cli.py`、根目录旧字幕工厂 6 文件、`legacy_tests/**`、
  `tests/test_word_video_*.py`、`AGENTS.md`、`control/**`、`docs/**`、其他 worktree、
  `C:\Users\Administrator\PycharmProjects\PythonSRT`、`D:\单词速记自动化_测试归档_0915`、`D:\jianying`

## 交付物

`packaging/build_member_package.py` 产出一个**目录式包**（不要求签名、不要求安装器）：

```
out\packages\word-video-member-<ver>\
  WordVideo\            # PyInstaller onedir 产物（自带 Python 运行时，入口 word_video_cli.py）
  ffmpeg\ffmpeg.exe ffprobe.exe
  单词视频.cmd           # 启动器：设置包内 PATH/TEMP，转发全部参数
  README-成员使用.md
  VERSION.txt           # 版本、构建时间、源 commit、文件数/体积
```

要点：
- 入口是**现有 headless CLI**，参数与 stdout 单 JSON 契约不变（不要包装成别的东西）。
- 启动器必须把 `TEMP/TMP` 指到包内或用户指定目录，不污染 C 盘；`PATH` 只加包内 `ffmpeg`。
- 包里**不得**含：任何密钥/环境变量值、真实素材、音频缓存、全量词表、剪映草稿、
  无分发依据的字体；`build_member_package.py` 要自带一次扫描并把结果打印到 stdout。

## 必须真跑的验收（贴命令与真实输出）

1. **清洗环境**下跑 `doctor`：PATH 只留 `C:\Windows\System32`（去掉 Python、去掉 FFmpeg、
   去掉开发机 venv），清空 `PYTHONHOME/PYTHONPATH/PYTHONSTARTUP`，工作目录设为包目录，
   运行 `单词视频.cmd doctor`：应得到单个 JSON、`ok=true`、字体无替换。
2. **同一清洗环境**跑三词真实离线作业：把 `data\fixtures\p1-无片头.json` 复制一份，
   `output` 改到包外 tmp，运行 `单词视频.cmd --db <包内或tmp db> submit --request <json>`
   再 `start --job <id>`：必须产出三产物齐全（MP4、五轨 SRT、editable-draft）且 `complete.json`
   里文件数与 `video_check` 正常。**重点验证 worker 子进程**：引擎用 `sys.executable` 起 worker，
   打包后它必须仍能启动（这是本任务最大的真实风险，必须实测，不能假设）。
3. **开发路径不可依赖**：整个第 1、2 步期间不得访问 `C:\Users\Administrator\PycharmProjects\PythonSRT`
   或 `runtime\venv`（从 PATH 与工作目录两个角度都排除），并记录你实际用的命令与环境变量。
4. **包内容扫描**：列出包内文件数/体积；证明无密钥、无真实素材、无音频缓存、无全量词表。
5. 启动耗时与包体积实测数字。

## 禁止事项

**只用绝对路径写文件**（子 Agent 默认 cwd 是 `D:\1\1-AI_workflow`，不是你的 worktree）。
不 pip 改共享 venv（要新依赖先停下问 H0）；不改生产代码来让打包通过；
不 force push、不 merge、不切分支；不递归派子 Agent；**重编码/长测试独占本机槽位**；
不自动下载任何 URL（缺什么先报 H0，不联网抓运行时）。

## 报告格式（只报这四项）

改了什么（含 commit hash） → 测了什么及实际结果（命令 + 真实输出摘要，含清洗环境证据） →
未验证什么（例如未在真正的第二台机器上验） → 下一步/回滚 commit。
