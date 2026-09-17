"""已退役的 W06 时间线交互测量入口（保留明确诊断，不再伪报现行验收）。

Tim 2026-09-17 下线软件内时间线编辑；旧入口依赖 timeline、split_button 和
连续预览 session，已不能测量当前窗口。历史证据可从 Git 查阅，不是新版本证据。
命令/排版/样式回归位于 tests/test_desktop_editor_{model,ui}.py 和
 tests/test_desktop_{style_panel,text_preview}.py；真实媒体与成员机观感仍需单独验收。
"""


def main(argv=None):
    raise SystemExit('W06 时间线交互测量已退役：当前窗口只有文字 + 背景静帧预览。'
                     '请运行当前 desktop 测试；这不替代 acceptance_media 或成员机验收。')


if __name__ == '__main__':
    main()
