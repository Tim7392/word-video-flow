"""已退役的 W04 视频/音频预览测量入口。

Tim 2026-09-17 改为文字 + 背景静帧预览；不再解码视频、建代理或打开音频设备。
旧的首视频帧/PCM seek 成本不是当前 GUI 的指标，历史实现与证据可从 Git 查阅。
文字绘制与静帧失败回归在 tests/test_desktop_text_preview.py；该回归不替代
真实媒体门禁、目标机性能或观感验收。
"""


def main(argv=None):
    raise SystemExit('W04 连续视频/音频预览测量已退役：请验证文字预览与静帧。'
                     '当前未执行真实媒体或目标机验收。')


if __name__ == '__main__':
    main()
