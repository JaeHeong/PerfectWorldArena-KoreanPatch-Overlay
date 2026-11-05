from pwa_overlay.dictionary import Dictionary, normalize


def test_normalize_folds_punctuation_and_space():
    assert normalize("确认 （3）！") == "确认(3)!"
    assert normalize("ＡＢＣ　平台") == "abc平台"


def test_exact(dictionary):
    assert dictionary.lookup("关闭").ko == "닫기"
    assert dictionary.lookup("退出平台").ko == "플랫폼 종료"


def test_exact_ignores_punctuation_width(dictionary):
    m = dictionary.lookup("是否将平台最小化到托盘？")
    assert m and m.method == "exact"


def test_fuzzy_tolerates_one_wrong_char(dictionary):
    m = dictionary.lookup("是否将平台最小化到拖盘?")  # 托 -> 拖
    assert m and m.method == "fuzzy"
    assert m.ko == "플랫폼을 트레이로 최소화할까요?"


def test_short_strings_not_fuzzed(dictionary):
    assert dictionary.lookup("关团") is None


def test_template(dictionary):
    m = dictionary.lookup("玩家 abc 已将房主转交给 def")
    assert m and m.method == "template"
    assert "abc" in m.ko and "def" in m.ko


def test_phrase_substitution_requires_full_coverage():
    d = Dictionary()
    d.add("开始", "시작")
    d.add("匹配", "매칭")
    d.finalize()
    assert d.lookup("开始匹配").ko == "시작 매칭"
    assert d.lookup("开始匹配吧") is None


def test_non_cjk_ignored(dictionary):
    assert dictionary.lookup("Steam 123") is None


def test_user_dict_overrides(tmp_path):
    (tmp_path / "m.json").write_text('{"mappings":[{"zh":"关闭","ko":"닫기"}]}', encoding="utf-8")
    user = tmp_path / "user.json"
    user.write_text('{"关闭": "창 닫기"}', encoding="utf-8")
    d = Dictionary.from_dir(tmp_path, user_dict=user)
    assert d.lookup("关闭").ko == "창 닫기"
