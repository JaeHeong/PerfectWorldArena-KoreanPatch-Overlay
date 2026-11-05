"""Regression tests for the review fixes: fuzzy guards, templates, grouping, label
splitting, ink-based invalidation, colours and mapping data quality."""

import json
import re
from pathlib import Path

import numpy as np
import pytest

from pwa_overlay.analyzer import (Line, _split_line, contrast, signature_changed,
                                  split_runs, translate_lines)
from pwa_overlay.dictionary import Dictionary

ROOT = Path(__file__).resolve().parents[1]


# ---- dictionary --------------------------------------------------------------------
@pytest.fixture
def d():
    d = Dictionary()
    d.add("5秒后自动关闭", "5초 후 자동으로 닫힙니다")
    d.add("当前账号与平台绑定账号不一致", "계정이 연동 계정과 일치하지 않습니다")
    d.add("是否将平台最小化到托盘?", "플랫폼을 트레이로 최소화할까요?")
    d.add("逃跑", "탈주")
    d.add_template(["玩家 ", "${var1}", " 已将房主转交给 ", "${var2}"],
                   ["플레이어 ", "${var1}", " 님이 방장을 ", "${var2}", " 님에게 넘겼습니다."])
    d.add_template(["因", "${var1}", "被冷却，", "${var2}"],
                   ["${var1}", " 사유로 쿨다운이 적용되었습니다. ", "${var2}"])
    d.finalize()
    return d


def test_fuzzy_never_changes_a_number(d):
    assert d.lookup("3秒后自动关闭") is None


def test_fuzzy_never_drops_a_negation(d):
    assert d.lookup("当前账号与平台绑定账号一致") is None


def test_fuzzy_still_fixes_one_ocr_typo(d):
    m = d.lookup("是否将平台最小化到拖盘?")
    assert m and m.method == "fuzzy"


def test_template_keeps_nickname_case_and_spaces(d):
    m = d.lookup("玩家 Mr Bean 已将房主转交给 Tom")
    assert m.ko == "플레이어 Mr Bean 님이 방장을 Tom 님에게 넘겼습니다."


def test_template_translates_chinese_reason_slot(d):
    assert d.lookup("因逃跑被冷却，30:00").ko == "탈주 사유로 쿨다운이 적용되었습니다. 30:00"


def test_template_with_chinese_left_in_any_slot_is_not_used(d):
    assert d.lookup("因逃跑被冷却，剩余30分钟") is None  # 剩余..分钟 unknown: whole line to Claude


def test_template_with_untranslatable_slot_goes_to_cache_or_claude(d):
    assert d.lookup("因外星人入侵被冷却，剩余30分钟") is None


def test_broken_json_reports_file_and_line(tmp_path):
    (tmp_path / "m.json").write_text('{"mappings": [{"zh": "关闭", "ko": "닫기"},]}',
                                     encoding="utf-8")
    with pytest.raises(ValueError, match=r"m\.json: 1번째 줄"):
        Dictionary.from_dir(tmp_path)


def test_glossary_only_from_curated_sources(tmp_path):
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "m.json").write_text(
        '{"mappings": [{"zh": "玩家", "ko": "계정"}]}', encoding="utf-8")
    (tmp_path / "glossary.json").write_text('{"天梯": "랭크"}', encoding="utf-8")
    (tmp_path / "user.json").write_text(
        '{"战令": "배틀패스", "OCR 오인식": {"首巅": "홈"}}', encoding="utf-8")
    d = Dictionary.from_dir(tmp_path / "mappings", tmp_path / "user.json")
    g = dict(d.glossary_for(["玩家天梯战令首巅"]))
    assert g == {"天梯": "랭크", "战令": "배틀패스"}   # no page label, no OCR alias
    assert d.lookup("首巅").ko == "홈"                 # but the alias still translates


# ---- grouping / OCR shaping --------------------------------------------------------
def L(x0, y0, x1, h, text):
    return Line(x0, y0, x1, y0 + h, text, 0.99)


def test_heading_split_from_its_description():
    group = [L(10, 10, 90, 18, "活动规则"),
             L(10, 32, 400, 18, "活动期间完成指定任务即可获得天赋点奖励，"),
             L(10, 54, 220, 18, "奖励将在活动结束后发放。")]
    runs = split_runs(group)
    assert [len(r) for r in runs] == [1, 2]


def test_numbered_list_items_are_separate_units():
    group = [L(10, 10, 400, 18, "1.每日签到可获得积分奖励，连续签到可获得"),
             L(10, 32, 200, 18, "额外奖励"),
             L(10, 54, 300, 18, "2.积分可在商城兑换道具"),
             L(10, 76, 300, 18, "3.活动最终解释权归平台所有")]
    assert [len(r) for r in split_runs(group)] == [2, 1, 1]


def test_merged_labels_split_by_character_boxes():
    chars = [(c, 0.95, 975 + 13 * i, 975 + 13 * i + 12) for i, c in enumerate("活动规则")] + \
            [(c, 0.9, 1041 + 13 * i, 1041 + 13 * i + 12) for i, c in enumerate("神马币：")]
    parts = _split_line(975, 65, 1094, 88, "活动规则 神马币：", 0.94, chars)
    assert [p.text for p in parts] == ["活动规则", "神马币："]
    assert parts[0].x1 <= 1030 and parts[1].x0 >= 1040


def test_date_and_time_keep_their_space():
    assert _split_line(0, 0, 10, 10, "开始时间:2026.10.2312:00", 1.0, None)[0].text == \
        "开始时间:2026.10.23 12:00"


# ---- invalidation and colours --------------------------------------------------------
def _frame_with_text(text_rows, bg=30, ink=225, size=(120, 400)):
    f = np.full((*size, 3), bg, np.uint8)
    for (y0, y1, x0, x1) in text_rows:
        f[y0:y1, x0:x1] = ink
    return f


class Exact:
    def lookup(self, text, **_):
        from pwa_overlay.dictionary import Match
        return Match("번역", "exact")


def test_one_changed_glyph_invalidates_but_background_animation_does_not():
    frame = _frame_with_text([(20, 36, 20, 24), (20, 36, 30, 34), (20, 36, 40, 44)])
    item = translate_lines(frame, [L(18, 18, 48, 20, "已完成次数")], Exact()).items[0]
    assert not signature_changed(frame, item)
    digit = frame.copy()
    digit[20:36, 40:44] = 30          # one stroke group disappears ("0/3" -> "1/3")
    digit[22:34, 41:47] = 225
    assert signature_changed(digit, item)
    glow = frame.copy()
    glow[(glow == 30).all(axis=2)] = 48  # background pulses +18
    assert not signature_changed(glow, item)


def test_low_contrast_text_gets_black_or_white():
    frame = np.full((40, 200, 3), 40, np.uint8)
    frame[12:28, 20:120] = 55  # text barely brighter than the background
    item = translate_lines(frame, [L(18, 10, 122, 20, "积分商城")], Exact()).items[0]
    assert contrast(item.fg, item.bg) >= 3.0


def test_text_on_artwork_is_flagged():
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (60, 200, 3), dtype=np.uint8)
    item = translate_lines(frame, [L(10, 10, 150, 30, "国服战令下")], Exact()).items[0]
    assert item.art


# ---- data quality ----------------------------------------------------------------------
LATIN_OK = {"steam", "vac", "ow", "pro", "vip", "cs", "cs2", "id", "app", "roll", "qq",
            "cncs", "pac", "mvp", "rank", "pvp", "fps", "ping", "online", "steamid", "dust",
            "ii", "bp", "f1", "w", "mm", "esea", "pw", "ai", "ui", "ok",
            "offline"}  # 隐身 shown as OFFLINE next to ONLINE, on purpose


def _entries():
    for f in sorted((ROOT / "data" / "mappings").glob("*.json")):
        for e in json.loads(f.read_text(encoding="utf-8"))["mappings"]:
            if isinstance(e.get("ko"), str):
                yield f.name, e.get("zh", ""), e["ko"]


def test_no_html_in_translations():
    bad = [(f, zh) for f, zh, ko in _entries() if re.search(r"</?\w+[^>]*>", ko)]
    assert bad == []


def test_no_stray_latin_words_in_translations():
    bad = []
    for f, zh, ko in _entries():
        for w in re.findall(r"[A-Za-z]{3,}", ko):
            if w.lower() not in LATIN_OK and w.lower() not in zh.lower():
                bad.append((f, zh, w))
    assert bad == []


def test_no_conflicting_duplicate_keys():
    from pwa_overlay.dictionary import normalize
    seen, bad = {}, []
    for f, zh, ko in _entries():
        k = normalize(zh)
        if k in seen and seen[k] != ko:
            bad.append(zh)
        seen[k] = ko
    assert bad == []


def test_label_with_hanging_indent_is_one_sentence():
    from pwa_overlay.analyzer import group_paragraphs, looks_wrapped
    lines = [L(527, 312, 745, 17, "描述：天梯比赛失败扣分导致段位"),
             L(575, 333, 732, 17, "下降一段时（惩罚扣分不"),
             L(574, 354, 746, 17, "算），自动扣除一个道具保"),
             L(573, 375, 753, 17, "护段位不会下降（不包含S段"),
             L(572, 395, 747, 20, "位及以上)，仅可在临渊加"),
             L(572, 416, 653, 20, "冕赛季使用。")]
    groups = group_paragraphs(lines)
    assert len(groups) == 1
    runs = split_runs(groups[0])
    assert len(runs) == 1 and looks_wrapped(runs[0])


def test_title_over_a_tag_is_not_a_wrapped_sentence():
    from pwa_overlay.analyzer import looks_wrapped
    assert not looks_wrapped([L(527, 140, 699, 23, "段位保护盾（临渊加冕）"),
                              L(534, 171, 585, 18, "平台道具")])


def test_bom_and_broken_files_never_stop_the_app(tmp_path):
    from pwa_overlay.config import Config
    from pwa_overlay.translator import TranslationCache
    bom = tmp_path / "config.json"
    bom.write_text('{"ocr_device": "gpu"}', encoding="utf-8-sig")   # Notepad / PowerShell style
    assert Config.load(bom).ocr_device == "gpu"
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    cfg = Config.load(bad)
    assert cfg.ocr_device == "cpu" and "ignored" in cfg.load_error
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "a.json").write_text('{"mappings": [{"zh": "关闭", "ko": "닫기"}]}',
                                         encoding="utf-8-sig")
    assert Dictionary.from_dir(tmp_path / "m").lookup("关闭").ko == "닫기"
    cache = tmp_path / "c.json"
    cache.write_text('{"k": {"zh": "关闭", "ko": "닫기", "at": "x"}}', encoding="utf-8-sig")
    assert TranslationCache(cache).get("关闭")["ko"] == "닫기"
