# 单词视频 · 成员使用说明（早期目录式包）

版本 **{{VERSION}}** ｜ 构建时间 {{BUILT}} ｜ 源提交 `{{COMMIT}}`

这个文件夹就是整个软件。**不需要安装 Python，不需要安装 FFmpeg，不需要配置 PATH。**
Python 运行时和 FFmpeg 都已经放在包里，启动器会把它们接上。

---

## 一、怎么开始（三步）

1. 把整个 `word-video-member-{{VERSION}}` 文件夹**整个复制**到你机器上可写的位置，
   例如 `D:\word-video\`（不要放在 C 盘系统目录、Program Files、只读共享盘或压缩包里）。
2. 在该文件夹里打开命令行（地址栏输入 `cmd` 回车即可），先自检：

   ```
   单词视频.cmd doctor
   ```

   看到一行 JSON 且开头是 `{"ok": true` 就算就绪，其中的 `ffmpeg` / `ffprobe`
   路径应当指向**本文件夹内的** `ffmpeg\` 目录。

3. 按下面的示例提交并开始一个作业。

> 提示：双击 `单词视频.cmd` 不会做任何事（它需要参数）。请始终在命令行里带参数运行。

## 二、做一个视频作业

准备一个请求 JSON（例如 `request.json`，UTF-8 编码），最小示例：

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
- `provider.kind` 用 `local` 表示“音频我已备好，只读这些文件”，此模式**不联网**。
  每个词的 `female` / `male` / `chinese` 三条音都要给全。
- `output` 指向你自己的输出目录，请放在可写的 D 盘位置。

提交并启动：

```
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 submit --request request.json
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 start  --job <上一步返回的 id>
单词视频.cmd --db D:\word-video-data\jobs.sqlite3 status --job <id>
```

`submit` 会返回 `id`。`start` 会立刻返回（内含 worker 进程号），真正干活的是后台
worker 进程；`status` 显示 `generated` 即完成。渲染较慢，请耐心等待，
中途不要重复 `start` 同一个作业。

`--db` 是作业数据库，放到你自己的数据目录里即可（**建议总是显式指定**，
否则会写进包内目录）。

## 三、产出在哪

每个作业在 `output\<作业id>\<序号区间>\` 下产出三件套：

```
video\video.mp4              成片
srt\*.srt                    五轨字幕（英文重复、英文单次、音标、中文带词性、中文无词性）
editable-draft\              可编辑剪映草稿（含所需媒体副本）
complete.json                本次提交的文件清单与自查结果
```

`complete.json` 里的 `files` 是全部交付文件及其校验值，`video_check` 是成片的机器自检。
**机器自检通过不等于可以发布**：请本人打开草稿核对，并完整听一遍。

## 四、临时文件与清理

- 启动器把 `TEMP` / `TMP` 指向**包内** `temp\` 目录，默认不在 C 盘留东西。
- 想换位置：先设环境变量再运行，例如
  `set WORD_VIDEO_TEMP=D:\word-video-temp` 然后照常运行 `单词视频.cmd`。
- 作业中途失败时，未提交的批次会移到该作业目录下的 `recovery\`，不会被静默删除。

## 五、请这样做 / 请不要这样做

**请这样做**
- 整个文件夹一起复制、一起搬；保持 `WordVideo\`、`ffmpeg\`、`单词视频.cmd` 的相对位置不变。
- 出错时把命令行输出的**整段 JSON**（以及作业目录下的 `*.worker.log`）发回来。

**请不要这样做**
- 不要只复制 `WordVideo.exe`（它必须和同目录的 `_internal\` 一起用）。
- 不要把包放在只读位置或网络盘上运行。
- 不要为了让命令跑起来去装 Python、改系统 PATH —— 需要什么就在群里说。

## 六、这一版已知限制（如实告知）

- **未签名**：首次运行 Windows 可能弹 SmartScreen 提示；未在真正的第二台干净电脑上验收过。
- 还没有图形界面，只能命令行提交作业。
- 需要本机已安装剪映才能打开 `editable-draft\`（本包不附带剪映）。
- 字体沿用本机已有字体：若你的机器缺少参考字体，`doctor` 的
  `font_substitutions` 会列出被替换的角色，**系统不会静默换字体**，请把结果发回来。
- 真实语音合成（TTS）需要账号凭据，本包**不含任何密钥**，也不进行任何文字转语音调用。
