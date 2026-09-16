"""The v2 spoken-text policy: what may be read aloud, derived from the display meaning.

Every expectation here is hand-written from the approved rule; the same expression
is what the independent M0 acceptance tool applies, so a case that passes here
passes the checker's re-derivation too (checked end to end in
``tests/test_wv_cleaning_pipeline.py`` and by running the checker itself).
"""
import pytest

from word_video import text_policy
from word_video.contracts import ALL_POLICIES, DEFAULT_POLICY
from word_video.text_policy import (POLICY_LEGACY, POLICY_SUPPLIED, POLICY_V2,
                                    leftover_connectors, spoken_from_meaning)


def test_the_approved_examples():
    # The two examples the approval names, plus the shapes around them.
    assert spoken_from_meaning('n. 文物adj. 陈旧的') == '文物 陈旧的'
    assert spoken_from_meaning('vt. 评估；估价') == '评估；估价'
    assert spoken_from_meaning('n. & v. 谋 杀') == '谋 杀'
    assert spoken_from_meaning('adj. 故意的 v. 仔细考虑') == '故意的 仔细考虑'
    assert spoken_from_meaning('adv. 事实上') == '事实上'
    assert spoken_from_meaning('n. 苹果') == '苹果'


def test_word_forms_that_only_look_like_tags_are_untouched():
    # A Latin letter before the tag is what protects a real word form.
    assert spoken_from_meaning('n. R&D 研发') == 'R&D 研发'
    assert spoken_from_meaning('n. A/B 测试') == 'A/B 测试'
    assert spoken_from_meaning('v. print. 打印') == 'print. 打印'
    assert spoken_from_meaning('n. and/or 与或') == 'and/or 与或'
    # "a." is not a tag the rule knows, so nothing is invented about it.
    assert spoken_from_meaning('a. 一个') == 'a. 一个'


def test_embedded_phonetics_are_removed_but_a_real_slash_is_kept():
    assert spoken_from_meaning('n. 内容；目录 /kənˈtent/ adj. 满意的') == '内容；目录 满意的'
    assert spoken_from_meaning('/ˈæpəl/ 苹果') == '苹果'
    assert spoken_from_meaning('n. and/or 与或') == 'and/or 与或'


def test_whitespace_collapses_and_ends_are_trimmed():
    assert spoken_from_meaning('n.   &   v.   谋杀 ') == '谋杀'
    assert spoken_from_meaning('  n. 苹果  ') == '苹果'
    assert spoken_from_meaning('n.  a \t b ') == 'a b'


def test_empty_and_tag_only_lines_do_not_invent_text():
    assert spoken_from_meaning('') == ''
    assert spoken_from_meaning('   ') == ''
    assert spoken_from_meaning('n.') == ''
    assert spoken_from_meaning('n. & v.') == ''
    assert spoken_from_meaning('n. v. adj.') == ''


def test_legacy_reproduces_the_delivered_behaviour():
    # Tags alone are removed and everything around them stays, so the connectors
    # and the double spaces of a delivered batch survive: this is what those
    # batches sound like.
    assert spoken_from_meaning('n. & v. 谋 杀', POLICY_LEGACY) == '&  谋 杀'
    assert spoken_from_meaning('adj. 故意的 v. 仔细考虑', POLICY_LEGACY) == '故意的  仔细考虑'
    assert spoken_from_meaning('n.  内容；目录 /kənˈtent/ adj. 满意的', POLICY_LEGACY) \
        == '内容；目录 /kənˈtent/  满意的'
    # A tag glued to Chinese counts as a tag in both policies.
    assert spoken_from_meaning('int. 天哪n. 善良', POLICY_LEGACY) == '天哪 善良'
    assert spoken_from_meaning('int. 天哪n. 善良') == '天哪 善良'


def test_the_legacy_and_v2_shapes_differ_where_they_should():
    """Four shapes, hand-computed from the rule both policies copy.

    The legacy core's own tag list is narrower (no ``int``/``aux``/``abbr``/``pl``,
    but ``a.``) and it also strips a bracketed phonetic, so a batch built from
    *its* ``d_c`` field keeps a tag this rule removes.  ``legacy`` here follows the
    checker's expression, so a reproduced batch is judged by the rule it was
    produced with; the report lists the four word-list lines where the two differ.
    """
    cases = {
        # meaning                       legacy (tags out, spacing kept)   v2 (tags+connectors)
        'n. & v. 谋 杀': ('&  谋 杀', '谋 杀'),
        '(pl. phenomena) n. 现 象': ('( phenomena)  现 象', '( phenomena) 现 象'),
        'aux. / v. 应当；应该': ('/  应当；应该', '应当；应该'),
        'v. / aux. 敢；敢于 n. 挑战；激将': ('/  敢；敢于  挑战；激将', '敢；敢于 挑战；激将'),
        'int. 天哪n. 善良；美德；精华': ('天哪 善良；美德；精华', '天哪 善良；美德；精华'),
    }
    for meaning, (legacy, v2) in cases.items():
        assert spoken_from_meaning(meaning, POLICY_LEGACY) == legacy, meaning
        assert spoken_from_meaning(meaning) == v2, meaning


def test_leftovers_are_reported_not_guessed():
    """Rule 5: a mark the rule cannot judge stays and is listed."""
    assert leftover_connectors('偏爱，喜爱') == ('，',)
    assert leftover_connectors('财产，资产；性质') == ('，',)
    assert leftover_connectors('谋 杀') == ()
    assert leftover_connectors('R&D') == ('&',)          # kept on purpose, listed
    assert leftover_connectors('木板 上（船、车）') == ('、',)
    # ; and ； are ordinary definition punctuation, never flagged as a connector.
    assert leftover_connectors('评估；估价') == ()


def test_unknown_policies_are_refused():
    for bad in ('V2', 'v3', '', None, 'supplied', 1):
        with pytest.raises(ValueError):
            spoken_from_meaning('n. 苹果', bad)
    assert text_policy.check_policy(POLICY_V2) == POLICY_V2
    assert text_policy.check_policy(POLICY_LEGACY) == POLICY_LEGACY
    with pytest.raises(ValueError):
        text_policy.check_policy(POLICY_SUPPLIED)


def test_policy_constants_are_the_contract():
    assert text_policy.DEFAULT_POLICY == 'v2'
    assert text_policy.POLICIES == ('v2', 'legacy')
    assert text_policy.ALL_POLICIES == ('v2', 'legacy', 'supplied')
    assert DEFAULT_POLICY == 'v2'
    assert ALL_POLICIES == ('v2', 'legacy', 'supplied')
    assert POLICY_V2 not in (POLICY_LEGACY, POLICY_SUPPLIED)


def test_non_string_input_is_refused():
    with pytest.raises(TypeError):
        spoken_from_meaning(None)
    with pytest.raises(TypeError):
        spoken_from_meaning(['n. 苹果'])
    with pytest.raises(TypeError):
        leftover_connectors(None)
