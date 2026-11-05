"""Sharing translation_cache.json between friends: export, then merge on import."""

import json

import pytest

from pwa_overlay.translator import TranslationCache, cache_key


def entry(zh, ko, at="2026-11-04 10:00:00", uncertain=False):
    return {"zh": zh, "ko": ko, "by": "claude-haiku-5-5", "at": at, "uncertain": uncertain}


def write(path, entries):
    path.write_text(json.dumps({f"k{i}": e for i, e in enumerate(entries)}, ensure_ascii=False),
                    encoding="utf-8")


def test_merge_adds_new_and_keeps_newer(tmp_path):
    mine = TranslationCache(tmp_path / "mine.json")
    mine.put_many([("已领取", "수령 완료", False), ("天赋点", "천부 포인트", False)], "me")
    shared = tmp_path / "shared.json"
    write(shared, [
        entry("立即前往", "바로 가기"),                                   # new
        entry("天赋点", "특성 포인트", at="2099-01-01 00:00:00"),          # newer: wins
        entry("已领取", "받음", at="2000-01-01 00:00:00"),                 # older: ignored
        entry("风中的少年", None),                                          # KEEP (nickname)
    ])
    added, updated, skipped = mine.merge_file(shared)
    assert (added, updated, skipped) == (2, 1, 0)
    assert mine.get("立即前往")["ko"] == "바로 가기"
    assert mine.get("天赋点")["ko"] == "특성 포인트"
    assert mine.get("已领取")["ko"] == "수령 완료"
    assert mine.get("风中的少年")["ko"] is None
    # and it is on disk for the next start
    assert TranslationCache(tmp_path / "mine.json").get("立即前往")["ko"] == "바로 가기"


def test_merge_skips_bad_entries(tmp_path):
    mine = TranslationCache(tmp_path / "mine.json")
    shared = tmp_path / "shared.json"
    write(shared, [
        entry("立即前往", "바로 가기"),
        entry("国服战令", "국服 배틀패스"),              # Chinese left in the Korean
        entry("个人天赋点：{0}/{1}", "개인 특성 포인트"),  # placeholders dropped
        {"zh": 123, "ko": "x"},                          # not an entry at all
    ])
    assert mine.merge_file(shared) == (1, 0, 3)


def test_not_a_cache_file_changes_nothing(tmp_path):
    mine = TranslationCache(tmp_path / "mine.json")
    mine.put_many([("已领取", "수령 완료", False)], "me")
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        mine.merge_file(bad)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        mine.merge_file(broken)
    assert len(mine) == 1


def test_export_then_import_on_a_friends_pc(tmp_path):
    mine = TranslationCache(tmp_path / "mine.json")
    mine.put_many([("已领取", "수령 완료", False), ("个人天赋点：{0}/{1}", "개인 특성 포인트: {0}/{1}", False)],
                  "me")
    out = tmp_path / "share.json"
    assert mine.export_to(out) == 2  # both are real translations
    friend = TranslationCache(tmp_path / "friend.json")
    assert friend.merge_file(out) == (2, 0, 0)
    assert friend.get("个人天赋点：{0}/{1}")["ko"] == "개인 특성 포인트: {0}/{1}"


def test_export_leaves_out_nicknames_kept_as_is(tmp_path):
    mine = TranslationCache(tmp_path / "mine.json")
    mine.put_many([("已领取", "수령 완료", False), ("风中的少年", None, False)], "me")
    out = tmp_path / "share.json"
    assert mine.export_to(out) == 1
    assert "风中的少年" not in out.read_text(encoding="utf-8")


def test_shared_cache_is_merged_at_startup(tmp_path):
    from pwa_overlay.dictionary import Dictionary
    from pwa_overlay.translator import make_translator
    (tmp_path / "data").mkdir()
    write(tmp_path / "data" / "translation_cache.shared.json", [entry("立即前往", "바로 가기")])
    d = Dictionary()
    d.finalize()
    t = make_translator(d, tmp_path, enabled=False, model="x")
    assert t.lookup("立即前往").ko == "바로 가기"


def test_exe_consumes_the_shared_copy_but_a_checkout_keeps_it(tmp_path):
    from pwa_overlay.translator import merge_shared_cache
    shared = tmp_path / "translation_cache.shared.json"
    write(shared, [entry("立即前往", "바로 가기")])
    mine = TranslationCache(tmp_path / "mine.json")
    merge_shared_cache(mine, shared, delete_after=False)
    assert shared.exists() and mine.get("立即前往")
    mine2 = TranslationCache(tmp_path / "mine2.json")
    merge_shared_cache(mine2, shared, delete_after=True)
    assert not shared.exists() and mine2.get("立即前往")
