"""CS2 item names paired from the game's own Chinese and Korean text (synthetic data
here: the real files stay on the player's PC)."""

from pwa_overlay.dictionary import Dictionary
from pwa_overlay.items import ItemNames, build_tables, parse_tokens
from pwa_overlay.translator import Translator

ZH = '''"lang" { "Tokens" {
"SFUI_WPNHUD_knife_ursus"	"熊刀"
"SFUI_WPNHUD_KnifeBayonet"	"刺刀"
"SFUI_WPNHUD_AK47"	"AK-47"
"CSGO_Wearable_v_slick_glove"	"驾驶手套"
"Button_Sticker"	"印花"
"SFUI_InvTooltip_Wear_Amount_0"	"崭新出厂"
"SFUI_InvTooltip_Wear_Amount_2"	"久经沙场"
"PaintKit_am_marbleized_Tag"	"多普勒"
"PaintKit_an_tiger_orange_Tag"	"虎牙"
"PaintKit_gs_ak47_gold_arabesque_Tag"	"黄金藤蔓"
"PaintKit_glove_dragon_Tag"	"龙拳"
"StickerKit_test_town"	"神秘小镇（闪亮）"
"StickerKit_test_town_desc"	"很长的说明文字"
"CSGO_Item_Desc_knife_ursus"	"这把刀"
} }'''
KO = '''"lang" { "Tokens" {
"SFUI_WPNHUD_knife_ursus"	"우르수스 칼"
"SFUI_WPNHUD_KnifeBayonet"	"총검"
"SFUI_WPNHUD_AK47"	"AK-47"
"CSGO_Wearable_v_slick_glove"	"운전용 장갑"
"Button_Sticker"	"스티커"
"SFUI_InvTooltip_Wear_Amount_0"	"막 출고된"
"SFUI_InvTooltip_Wear_Amount_2"	"현장에서 쓰인"
"PaintKit_am_marbleized_Tag"	"도플러"
"PaintKit_an_tiger_orange_Tag"	"호랑이 이빨"
"PaintKit_gs_ak47_gold_arabesque_Tag"	"황금 아라베스크"
"PaintKit_glove_dragon_Tag"	"용 가죽"
"StickerKit_test_town"	"블랙사이트(은박)"
"StickerKit_test_town_desc"	"긴 설명"
"CSGO_Item_Desc_knife_ursus"	"이 칼은"
} }'''


def names() -> ItemNames:
    return ItemNames(build_tables(parse_tokens(ZH), parse_tokens(KO)))


def test_market_names():
    n = names()
    assert n.lookup("熊刀（★）| 多普勒") == "우르수스 칼 (★) | 도플러"
    assert n.lookup("刺刀（★）| 虎牙") == "총검 (★) | 호랑이 이빨"
    assert n.lookup("AK-47 | 黄金藤蔓 (久经沙场)") == "AK-47 | 황금 아라베스크 (현장에서 쓰인)"
    assert n.lookup("StatTrak™ AK-47 | 黄金藤蔓 (崭新出厂)") == \
        "StatTrak™ AK-47 | 황금 아라베스크 (막 출고된)"
    assert n.lookup("印花 | 神秘小镇（闪亮）") == "스티커 | 블랙사이트(은박)"


def test_ocr_slips():
    n = names()
    assert n.lookup("AK-47l黄金藤蔓 (久经沙场)") == "AK-47 | 황금 아라베스크 (현장에서 쓰인)"
    assert n.lookup("驾驶手套（★）| 龙拳 (久经…") == "운전용 장갑 (★) | 용 가죽 (현장에서 쓰인)"
    assert n.lookup("驾驶手奎（★）| 龙拳") == "운전용 장갑 (★) | 용 가죽"   # one wrong char


def test_plain_words_are_not_taken_for_skins():
    n = names()
    assert n.lookup("崭新出厂") == "막 출고된"   # conditions and weapons stand alone
    assert n.lookup("熊刀") == "우르수스 칼"
    assert n.lookup("虎牙") is None             # also the streaming site Huya
    assert n.lookup("虎牙直播") is None
    assert n.lookup("刺刀 | 不存在的皮肤") is None
    assert "很长" not in str(n.right) and "这把刀" not in str(n.left)   # no descriptions


def test_item_names_come_before_cache_and_dictionary(tmp_path):
    d = Dictionary()
    d.add("熊刀", "베어 나이프")
    d.finalize()
    t = Translator(d, tmp_path / "c.json", None)
    t.cache.put_many([("刺刀（★）| 虎牙", "나이프 (★) | 타이거 투스", False)], "old")
    assert t.lookup("熊刀").ko == "베어 나이프"            # before the names load
    t.items = names()
    assert t.lookup("熊刀").ko == "우르수스 칼"
    assert t.lookup("刺刀（★）| 虎牙").ko == "총검 (★) | 호랑이 이빨"
