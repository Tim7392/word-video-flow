"""兼容保留的 PCM sink 契约；文字绘制行为已迁到 test_desktop_text_preview。

旧视频画布的 decoded-buffer、逐帧 timer 测试随控件退役删除。
音频适配器尚未删除，因此不顺便丢掉其独立覆盖；它不是当前窗口的依赖。
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtMultimedia, QtWidgets
import pytest
from test_preview_support import RATE


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_an_audio_sink_is_created_for_the_preview_rate_and_can_be_closed(app):
    from desktop.audio_qt import QtAudioOutput
    output = QtAudioOutput(rate=RATE, buffer_frames=960)
    try:
        assert output.format.sampleRate() == RATE
        assert output.format.channelCount() == 1
        assert output.format.sampleFormat() == QtMultimedia.QAudioFormat.Int16
        assert output.frames_played() == 0
        assert output.write(b'\x00\x00') == 0
    finally:
        output.close()
    assert output.report()['state'] is None
