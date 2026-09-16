# 单词视频 · 成员使用说明（目录式包，带编辑器）

版本 **{{VERSION}}** ｜ 构建时间 {{BUILT}} ｜ 源提交 `{{COMMIT}}`

这个文件夹就是整个软件。**不需要安装 Python，不需要安装 FFmpeg，不需要配置 PATH。**
Python 运行时、Qt 界面库和 FFmpeg 都已经放在包里，启动器会把它们接上。

---

## 一、先做一件事：把整个文件夹复制到本机

把 `word-video-member-{{VERSION}}` **整个文件夹**复制到你机器上可写的位置，例如 `D:\word-video\`。
不要放在 C 盘系统目录、Program Files、只读共享盘或压缩包里。

复制完以后，包里应该有这五样东西（位置不要改）：

```
WordVideo\            命令行引擎（Agent 用，见第五节）
WordVideoEditor\      编辑器窗口（成员用）
ffmpeg\               随包的视频工具
单词视频.cmd           启动器
README-成员使用.md     本文件
```

## 二、打开编辑器（成员的主要用法）

**双击 `单词视频.cmd`**，编辑器窗口就打开了（不会再弹出一个黑色命令行窗口）。

窗口里做一件事的三步：

1. **文件 → 从请求导入三词工程…**
   先选一个请求 JSON（下节说明怎么准备），再选一个工程目录（会写进这个目录）。
   导入**不联网**、不做语音合成，只读你已有的配音文件。
2. **看着预览改**：时间线上拖动/修剪/拆分，右边属性面板改字号等；
   撤销 `Ctrl+Z`、重做 `Ctrl+Y`、保存 `Ctrl+S`。改的时候不需要等整段重新编码。
3. **文件 → 导出三产物**（`Ctrl+E`）：MP4 + 五轨 SRT + 可编辑剪映草稿，
   同时把当前修订先存盘。导出在后台跑，界面不会卡住。

被拦下时窗口底部会出现一张卡片，写清楚**是哪个词、哪个文件、差多少**，
并给出可以点的修复按钮（撤销/定位/重新检查）。**它不会只弹一个"确定"。**

## 三、准备请求 JSON

编辑器导入的请求 JSON 和命令行是同一份格式（UTF-8 编码），最小示例：

```json
{
  "idempotency_key": "lesson-151-153",
  "source": { "path": "D:\\wordlists\\四级核心1500词_已清理.txt" },
  "range": { "start": 151, "end": 153 },
  "output": "D:\\word-video-out\\第一课",
  "lesson": {
    "background": "D:\\word-video-assets\\background-1080p.mp4",
    "batch_size": 50,
    "speed": 1.25,
    "width": 1920,
    "height": 1080,
    "video_codec": "h265"
  },
  "provider": {
    "kind": "local",
    "items": [
      { "index": 151, "role": "female",  "text": "demonstrate", "voice": "BV503_streaming", "path": "D:\\word-video-audio\\151-female.ogg" },
      { "index": 151, "role": "male",    "text": "demonstrate", "voice": "BV504_streaming", "path": "D:\\word-video-audio\\151-male.ogg" },
      { "index": 151, "role": "chinese", "text": "证明；证实",   "voice": "BV406_streaming", "path": "D:\\word-video-audio\\151-chinese.ogg" }
    ]
  }
}
```

说明：

- `source.path` 支持 `.txt` 和 `.docx` 词表；`range` 是 1 起、含首尾的序号范围。
- `provider.kind` 用 `local` 表示"音频我已备好，只读这些文件"，此模式**不联网**。
  每个词的 `female` / `male` / `chinese` 三条音都要给全。
- `output` 指向你自己的输出目录（导出的默认位置，也可以在导出时另选），放在可写的 D 盘位置。

## 四、产出在哪

每个工程导出到 `输出目录\rev<修订号>\` 下：

```
video\video.mp4              成片
srt\*.srt                    五轨字幕（英文重复、英文单次、音标、中文带词性、中文无词性）
editable-draft\              可编辑剪映草稿（含所需媒体副本）
captions.ass、timeline.json  字幕与时间线
complete.json                本次提交的文件清单与自查结果
```

`complete.json` 里的 `files` 是全部交付文件及其校验值，`video_check` 是成片的机器自检。
**机器自检通过不等于可以发布**：请本人打开草稿核对，并完整听一遍。

## 五、命令行（Agent / 批量，用法和以前完全一样）

`单词视频.cmd` **带参数**时走的是原来的命令行引擎，输出仍然是一行 JSON：

```
单词视频.cmd doctor
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 submit --request request.json
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 start  --job <上一步返回的 id>
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 status --job <id>
```

`doctor` 返回一行以 `{"ok": true` 开头的 JSON 就算就绪，其中的 `ffmpeg` / `ffprobe`
路径应当指向**本文件夹内的** `ffmpeg\` 目录。`--db` 是作业数据库，**建议总是显式指定**，
否则会写进包内目录。

**记住这一条就够：不带参数开编辑器，带参数是命令行。**

## 六、自动化 / 排障：不开窗口也能走编辑器的同一条路

带 `--editor` 时参数交给编辑器。它走的是**窗口里的同一条导入与导出路径**，
只是不用点鼠标，并把过程写成一份 JSON 记录（`--report`）：

```
单词视频.cmd --editor --import request.json --into D:\word-video-proj\第一课 --export --report D:\run.json
```

- `--import` + `--into`：导入请求并写入工程目录；`--open <工程目录>` 换成打开已有工程。
- `--export`：导入/打开后立刻导出三产物，结束并返回退出码（0 成功、1 失败）。
- `--report <文件>`：本次运行的 JSON 记录（启动耗时、导入耗时、导出耗时、产物清单、
  窗口与预览状态、被拦下的原因）。**报障时把这个文件发回来。**
- 不带 `--export` 时只是把工程打开在窗口里，留给你自己看。
- 想在没有人看着屏幕的机器上跑，先设 `set QT_QPA_PLATFORM=offscreen`。

## 七、临时文件与清理

- 启动器把 `TEMP` / `TMP` 指向**包内** `temp\` 目录，默认不在 C 盘留东西。
- 想换位置：先设环境变量再运行，例如
  `set WORD_VIDEO_TEMP=D:\word-video-temp` 然后照常运行 `单词视频.cmd`。
- 编辑器预览用的代理缓存放在**工程文件夹内** `.preview\`，有大小上限；
  删掉它只会让下次打开慢一次，不影响任何交付。
- 作业中途失败时，未提交的批次会移到该作业目录下的 `recovery\`，不会被静默删除。

## 八、请这样做 / 请不要这样做

**请这样做**
- 整个文件夹一起复制、一起搬；保持 `WordVideo\`、`WordVideoEditor\`、`ffmpeg\`、
  `单词视频.cmd` 的相对位置不变。
- 出错时把窗口底部卡片的内容、或第六节的 JSON 记录发回来。

**请不要这样做**
- 不要只复制某一个 `.exe`（它必须和同目录的 `_internal\` 一起用）。
- 不要把包放在只读位置或网络盘上运行。
- 不要为了让命令跑起来去装 Python、改系统 PATH —— 需要什么就在群里说。

## 九、这一版已知限制（如实告知）

- **未签名**：首次运行 Windows 可能弹 SmartScreen 提示；未在真正的第二台干净电脑上验收过。
- 编辑器是**窗口程序，没有命令行窗口**：如果它在启动阶段就退出，不会有黑底报错，
  请用第六节的 `--report` 跑一次并把 JSON 记录发回来。
- 需要本机已安装剪映才能打开 `editable-draft\`（本包不附带剪映）。
- 字体沿用本机已有字体：若你的机器缺少参考字体，`doctor` 的
  `font_substitutions` 会列出被替换的角色，**系统不会静默换字体**，请把结果发回来。
- 真实语音合成（TTS）需要账号凭据，本包**不含任何密钥**，也不进行任何文字转语音调用。
