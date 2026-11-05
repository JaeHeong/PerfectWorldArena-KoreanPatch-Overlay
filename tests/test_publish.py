"""packaging/publish_cache.py: what reaches the shared cache."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_publish_groups(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("publish_cache",
                                                  ROOT / "packaging" / "publish_cache.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path
    (root / "data").mkdir()
    def entry(zh, ko, **kw):
        return {"zh": zh, "ko": ko, "at": "2026-01-01 00:00:00", "by": "x", "uncertain": False, **kw}
    local = {
        "a": entry("已经共享的文字", "이미 공유됨", kind="ui", seen=1),
        "b": entry("常见菜单文字", "자주 보는 메뉴", kind="ui", seen=7),
        "c": entry("偶尔看到的文字", "가끔 본 문구", kind="ui", seen=2),
        "d": entry("帖子标题内容", "게시글 제목", kind="user", seen=9),
        "e": entry("以前缓存的文字", "예전 캐시", seen=9),
        "f": entry("批准的候选文字", "승인된 후보", kind="ui", seen=1),
        "g": entry("某个昵称", None),
    }
    src = root / "local.json"
    src.write_text(json.dumps(local, ensure_ascii=False), encoding="utf-8")
    (root / "data" / "translation_cache.shared.json").write_text(
        json.dumps({"x": entry("已经共享的文字", "이미 공유됨")}, ensure_ascii=False),
        encoding="utf-8")
    (root / "data" / "cache_allowlist.txt").write_text("批准的候选文字\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(sys, "argv", ["publish_cache.py", str(src)])
    assert mod.main() == 0
    shared = json.loads((root / "data" / "translation_cache.shared.json").read_text(encoding="utf-8"))
    assert {e["zh"] for e in shared.values()} == {"已经共享的文字", "常见菜单文字", "批准的候选文字"}
    assert all("seen" not in e for e in shared.values())
    review = (root / "build" / "cache_review.tsv").read_text(encoding="utf-8")
    assert "偶尔看到的文字" in review and "以前缓存的文字" in review
    assert "帖子标题内容" not in review and "某个昵称" not in review
