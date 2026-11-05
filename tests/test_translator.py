import time

import anthropic
import httpx2 as httpx
import numpy as np
import pytest

from pwa_overlay.analyzer import Line, looks_wrapped, translate_lines
from pwa_overlay.dictionary import Dictionary
from pwa_overlay.translator import (BatchError, ClaudeBackend, Translator, Usage, cache_key,
                                    mask_numbers, unmask)


class FakeBackend:
    """Stands in for ClaudeBackend: records calls, 'translates' by table."""

    model = "fake"
    anthropic = anthropic

    def __init__(self, table=None, error=None, refuse_if=None):
        self.table = table or {}
        self.error = error
        self.refuse_if = refuse_if
        self.calls = []
        self.usage = Usage()

    def translate(self, texts, glossary, screen, uncertain=None, budgets=None, retry=False):
        self.calls.append(dict(texts=list(texts), glossary=list(glossary), screen=list(screen),
                               uncertain=list(uncertain or []), budgets=list(budgets or []),
                               retry=retry))
        self.usage.add(100, 10, 0.1, 0.5)
        if self.error:
            raise self.error
        if self.refuse_if and any(self.refuse_if in t for t in texts):
            raise BatchError("refusal")
        return {i: self.table[t] for i, t in enumerate(texts) if t in self.table}


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def small_dict():
    d = Dictionary()
    d.add("天梯", "랭크", glossary=True)
    d.add("关闭", "닫기")
    d.add("完美电竞大会员", "완미 VIP 멤버십")
    d.add("已完成次数:{0}/{1}", "완료 횟수: {0}/{1}")
    d.finalize()
    return d


def make(tmp_path, d, be, **kw):
    return Translator(d, tmp_path / "c.json", be, batch_wait_s=kw.pop("wait", 0.02), **kw)


def test_mask_roundtrip():
    masked, nums = mask_numbers("个人天赋点：5/20")
    assert masked == "个人天赋点：{0}/{1}" and nums == ["5", "20"]
    assert unmask("개인 특성 포인트: {0}/{1}", nums) == "개인 특성 포인트: 5/20"
    assert unmask("개인 특성 포인트", nums) is None  # dropped a number


def test_cache_key_ignores_ocr_decoration():
    assert cache_key("活动规则:") == cache_key("活动规则") == cache_key("• 活动规则")
    assert cache_key("国服战令·下") == cache_key("国服战令下")
    assert cache_key("已领取") != cache_key("待领取")


def test_dictionary_first_no_api_call(tmp_path, small_dict):
    be = FakeBackend()
    t = make(tmp_path, small_dict, be)
    assert t.lookup("关闭").ko == "닫기"
    time.sleep(0.1)
    assert be.calls == []
    t.close()


def test_user_dict_with_number_placeholders(tmp_path, small_dict):
    t = make(tmp_path, small_dict, None)
    assert t.lookup("已完成次数:2/3").ko == "완료 횟수: 2/3"


def test_batch_then_cache_then_persist(tmp_path, small_dict):
    be = FakeBackend({"天赋点待领取": "특성 포인트 수령 대기",
                      "个人天赋点：{0}/{1}": "개인 특성 포인트: {0}/{1}",
                      "在一局天梯比赛中累积击杀{0}名敌人": "랭크 경기 한 판에서 적 {0}명 처치"})
    t = make(tmp_path, small_dict, be, wait=0.05)
    ctx = ("天赋点待领取", "个人天赋点：5/20", "SEASON")
    for text in ("天赋点待领取", "个人天赋点：5/20", "在一局天梯比赛中累积击杀20名敌人"):
        assert t.lookup(text, context=ctx) is None  # queued, not blocking
    assert wait_for(lambda: t.version > 0)
    assert len(be.calls) == 1                       # one request for the screen
    assert ("天梯", "랭크") in be.calls[0]["glossary"]  # curated term passed along
    assert t.lookup("天赋点待领取").ko == "특성 포인트 수령 대기"
    assert t.lookup("个人天赋点：7/20").ko == "개인 특성 포인트: 7/20"  # numbers reused
    t.close()

    be2 = FakeBackend()
    t2 = make(tmp_path, small_dict, be2)  # a new process: served from disk, no call
    assert t2.lookup("在一局天梯比赛中累积击杀35名敌人").ko == "랭크 경기 한 판에서 적 35명 처치"
    time.sleep(0.1)
    assert be2.calls == []
    t2.close()


def test_nickname_kept_and_never_sent_again(tmp_path, small_dict):
    be = FakeBackend({"风中的少年": ""})
    t = make(tmp_path, small_dict, be)
    t.lookup("风中的少年")
    assert wait_for(lambda: t.version > 0)
    assert t.lookup("风中的少年") is None   # nothing drawn over the name
    time.sleep(0.1)
    assert len(be.calls) == 1               # and never asked again
    t.close()


def test_failures_back_off_persistently(tmp_path, small_dict):
    be = FakeBackend({"个人天赋点：{0}/{1}": "개인 특성 포인트"})  # drops the numbers
    t = make(tmp_path, small_dict, be)
    t.lookup("个人天赋点：5/20")
    assert wait_for(lambda: len(be.calls) == 1)
    time.sleep(0.1)
    t.close()
    t2 = make(tmp_path, small_dict, be)    # restart: still backed off
    t2.lookup("个人天赋点：5/20")
    time.sleep(0.15)
    assert len(be.calls) == 1
    t2.close()


def test_refusal_splits_batch_and_keeps_good_lines(tmp_path, small_dict):
    table = {f"界面文字{c}": f"화면 문구 {n}" for n, c in enumerate("一二三四五")}
    be = FakeBackend(table, refuse_if="界面文字三")
    t = make(tmp_path, small_dict, be, wait=0.05)
    for k in table:
        t.lookup(k)
    assert wait_for(lambda: t.version > 0 and t.pending_count() == 0)
    assert t.lookup("界面文字一").ko == "화면 문구 0"
    assert t.lookup("界面文字五").ko == "화면 문구 4"
    assert t.lookup("界面文字三") is None
    assert t.failures.blocked(cache_key("界面文字三"))
    t.close()


def test_hourly_cap_stops_calls(tmp_path, small_dict):
    be = FakeBackend({"界面文字一": "가", "界面文字二": "나"})
    t = make(tmp_path, small_dict, be, max_calls_per_hour=1)
    t.lookup("界面文字一")
    assert wait_for(lambda: t.version > 0)
    t.lookup("界面文字二")
    time.sleep(0.2)
    assert len(be.calls) == 1
    assert "한도" in t.status
    t.close()


def test_bad_key_falls_back_to_dictionary(tmp_path, small_dict):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req),
                                        body=None)
    t = make(tmp_path, small_dict, FakeBackend(error=err))
    t.lookup("天赋点待领取")
    assert wait_for(lambda: not t.machine_translation)
    assert "API 키" in t.status
    assert t.lookup("关闭").ko == "닫기"
    t.close()


def test_mixed_output_retried_once_for_those_lines_only(tmp_path, small_dict):
    class Leaky(FakeBackend):
        def translate(self, texts, glossary, screen, uncertain=None, budgets=None, retry=False):
            super().translate(texts, glossary, screen, uncertain, budgets, retry)
            return {i: ("중국 서버 배틀패스" if retry else
                        ("국服 배틀패스" if t == "国服战令" else "닫기 버튼"))
                    for i, t in enumerate(texts)}

    be = Leaky()
    t = make(tmp_path, small_dict, be, wait=0.05)
    t.lookup("国服战令")
    t.lookup("关闭按钮")
    assert wait_for(lambda: t.version > 0)
    assert [c["retry"] for c in be.calls] == [False, True]
    assert be.calls[1]["texts"] == ["国服战令"] and be.calls[1]["screen"] == []
    assert t.lookup("国服战令").ko == "중국 서버 배틀패스"
    t.close()


def test_uncertain_short_line_needs_two_sightings(tmp_path, small_dict):
    be = FakeBackend({"赛零重雪卡": "시즌 초기화권"})
    t = make(tmp_path, small_dict, be)
    t.lookup("赛零重雪卡", uncertain=True)
    time.sleep(0.1)
    assert be.calls == []                    # one shaky read is not worth a call
    t.lookup("赛零重雪卡", uncertain=True)
    assert wait_for(lambda: len(be.calls) == 1)
    assert be.calls[0]["uncertain"] == [True]
    t.close()


def test_confident_characters_pin_down_a_known_string(tmp_path, small_dict):
    t = make(tmp_path, small_dict, None)
    chars = tuple(zip("完美电局大信员", (0.99, 0.98, 0.99, 0.17, 0.99, 0.28, 0.99)))
    m = t.lookup("完美电局大信员", uncertain=True, chars=chars)
    assert m and m.method == "fuzzy-ocr" and m.ko == "완미 VIP 멤버십"


def test_prompt_keeps_context_and_targets_apart():
    content = ClaudeBackend.build_prompt(
        ["赛零重雪卡", "系统检测您尚未登录Steam/蒸汽平台，请您开启并登录后重试"],
        [("天梯", "랭크")],
        ["改名卡", "赛零重雪卡", "SEASONR", "系统检测您尚未登录Steam/蒸汽平台，请您开",
         "启并登录后重试", "改名卡"],
        [True, False], [8, 0], False)
    context = content.split("(do NOT translate these):\n")[1].split("\n\n")[0].splitlines()
    targets = content.split("Translate these lines:\n")[1].splitlines()
    assert context == ["改名卡", "SEASONR"]       # in order, deduped, no targets/pieces
    assert targets[0].startswith("[100] 赛零重雪卡") and "[OCR uncertain]" in targets[0]
    assert "(≤8자)" in targets[0]
    assert targets[1].startswith("[101] 系统检测")


def test_schema_only_allows_this_requests_ids():
    from pwa_overlay.translator import output_schema
    ids = output_schema(3)["properties"]["translations"]["items"]["properties"]["id"]
    assert ids["enum"] == [100, 101, 102]


def test_without_backend_phrase_fallback_still_works(tmp_path):
    d = Dictionary()
    d.add("开始", "시작")
    d.add("匹配", "매칭")
    d.finalize()
    t = Translator(d, tmp_path / "c.json", None)
    assert t.lookup("开始匹配").ko == "시작 매칭"


def _line(x0, y0, x1, h, text):
    return Line(x0, y0, x1, y0 + h, text, 0.99)


def test_wrapped_paragraph_sent_whole(tmp_path, small_dict):
    para = [_line(10, 10, 400, 18, "系统检测您尚未登录Steam/蒸汽平台，请您开"),
            _line(10, 32, 200, 18, "启并登录后重试")]
    labels = [_line(10, 100, 80, 18, "首战告捷"), _line(10, 122, 300, 14, "赢得一场天梯模式的胜利")]
    assert looks_wrapped(para) and not looks_wrapped(labels)

    be = FakeBackend()
    t = make(tmp_path, small_dict, be, wait=0.05)
    translate_lines(np.zeros((200, 500, 3), np.uint8), para + labels, t)
    assert wait_for(lambda: len(be.calls) == 1)
    sent = be.calls[0]["texts"]
    assert "系统检测您尚未登录Steam/蒸汽平台，请您开启并登录后重试" in sent
    assert "启并登录后重试" not in sent           # not split line by line
    assert "首战告捷" in sent
    t.close()


def test_blank_answer_for_a_real_ui_line_is_not_kept(tmp_path, small_dict):
    be = FakeBackend({"全服天梯人数：{0}": ""})  # contains a number and a game term
    t = make(tmp_path, small_dict, be)
    t.lookup("全服天梯人数：123")
    assert wait_for(lambda: len(be.calls) == 1)
    time.sleep(0.1)
    assert t.cache.get("全服天梯人数：{0}") is None          # not cached as KEEP...
    assert t.failures.blocked(cache_key("全服天梯人数：{0}"))  # ...retried later instead
    t.close()


def test_one_character_labels_with_latin_or_numbers_are_sent(tmp_path, small_dict):
    be = FakeBackend({"ROLL房": "ROLL방", "{0}天": "{0}일"})
    t = make(tmp_path, small_dict, be, wait=0.05)
    for text in ("ROLL房", "93天", "永"):
        t.lookup(text)
    assert wait_for(lambda: t.version > 0)
    assert sorted(be.calls[0]["texts"]) == ["ROLL房", "{0}天"]  # the lone 永 is not sent
    assert t.lookup("7天").ko == "7일"
    t.close()


def test_shaky_line_on_a_static_page_is_rechecked_without_new_ocr(tmp_path, small_dict):
    be = FakeBackend({"赛零重雪卡": "시즌 초기화권"})
    t = make(tmp_path, small_dict, be)
    t.RECHECK_S = 0.1
    v = t.version
    t.lookup("赛零重雪卡", uncertain=True)       # first sighting: deferred
    assert wait_for(lambda: t.version > v)        # app gets a nudge to look again
    t.lookup("赛零重雪卡", uncertain=True)       # the app's re-run on the same OCR
    assert wait_for(lambda: len(be.calls) == 1)
    t.close()


def test_key_is_read_from_windows_environment(monkeypatch, tmp_path, small_dict):
    from pwa_overlay import translator as tr
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(tr, "load_key_from_registry", lambda: False)
    t = tr.make_translator(small_dict, tmp_path, True, "claude-haiku-5-5")
    assert not t.machine_translation and "API 키 없음" in t.status


def test_sentence_answered_without_korean_is_rejected(tmp_path, small_dict):
    be = FakeBackend({"描述：天梯比赛失败扣分导致段位下降": "PERFECT WORLD ARENA"})
    t = make(tmp_path, small_dict, be)
    t.lookup("描述：天梯比赛失败扣分导致段位下降")
    assert wait_for(lambda: len(be.calls) == 1)
    time.sleep(0.1)
    assert t.lookup("描述：天梯比赛失败扣分导致段位下降") is None   # not cached
    t.close()


def test_nickname_sentence_learned_as_template(tmp_path, small_dict):
    line = "玩家 Wind 加入了房间"
    be = FakeBackend({line: ("플레이어 Wind 님이 방에 입장했습니다",
                             "玩家 {n0} 加入了房间", "플레이어 {n0} 님이 방에 입장했습니다")})
    t = make(tmp_path, small_dict, be)
    t.lookup(line)
    assert wait_for(lambda: t.version > 0)
    assert t.lookup("玩家 Wind 加入了房间").ko == "플레이어 Wind 님이 방에 입장했습니다"
    m = t.lookup("玩家 xX_Pro_Xx 加入了房间")      # another player: no new call
    assert m.ko == "플레이어 xX_Pro_Xx 님이 방에 입장했습니다"
    time.sleep(0.1)
    assert len(be.calls) == 1
    t.close()
    raw = (tmp_path / "c.json").read_text(encoding="utf-8")
    assert "Wind" not in raw and "{n0}" in raw     # the shape is stored, not the name


def test_template_that_does_not_reproduce_the_line_is_dropped(tmp_path, small_dict):
    line = "玩家 Wind 加入了房间"
    be = FakeBackend({line: ("플레이어 Wind 님이 방에 입장했습니다",
                             "玩家 {n0} 离开了房间", "플레이어 {n0} 님이 퇴장했습니다")})
    t = make(tmp_path, small_dict, be)
    t.lookup(line)
    assert wait_for(lambda: t.version > 0)
    assert t.lookup(line).ko == "플레이어 Wind 님이 방에 입장했습니다"  # stored as a sentence
    assert t.lookup("玩家 Other 离开了房间") is None
    t.close()


def test_template_needs_a_real_fixed_part():
    from pwa_overlay.translator import compile_template
    assert compile_template("{n0}：{n1}") is None
    assert compile_template("{n0}了") is None
    assert compile_template("{n0}的包") is None
    assert compile_template("恭喜 {n0} 获得") is not None
