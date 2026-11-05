"""One-off: turn cached sentences that contain player/streamer names into templates
(names -> {n0}) and drop broken fragments, in the given cache files.

    uv run python packaging/migrate_name_templates.py data/translation_cache.json ...
"""

import sys
from pathlib import Path

from pwa_overlay.translator import TranslationCache, cache_key, compile_template

D = '您在{0}-{1}-{2}:{3}针对玩家"'   # report notices: "you reported player X at <date>"
K = '{0}-{1}-{2} {3}에 플레이어 "'
D2 = '您在{0}-{1}-{2} {3}:{4}针对玩家"'   # the same notices with a full time
K2 = '{0}-{1}-{2} {3}:{4}에 플레이어 "'

TEMPLATES = {
    "{n0}正在天梯激战": "{n0} 랭크 대전 중",
    "{n0}(游戏中)": "{n0} (게임 중)",
    "{n0}抖音直播": "{n0} 더우인 라이브",
    "{n0}抖音直播天梯匹配": "{n0} 더우인 라이브 랭크 매칭",
    "道具作者：{n0}": "아이템 제작자: {n0}",
    "完美世界电竞APP昵称{n0}": "완미 e스포츠 APP 닉네임 {n0}",
    D + '{n0}"的': K + '{n0}"에 대한',
    D + '{n0}"的举报正在处理': K + '{n0}"에 대한 신고가 처리 중입니다',
    D + '{n0}"的举报正在处理中......点击查看举':
        K + '{n0}"에 대한 신고가 처리 중입니다...... 클릭하여 신고 상세 보기',
    D + '{n0}"的举报系统自动处': K + '{n0}"에 대한 신고가 시스템에서 자동 처리',
    D + '{n0}"的举报系统自动处理已完成，点':
        K + '{n0}"에 대한 신고가 시스템에서 자동 처리 완료되었습니다. 클릭하여',
    D + '{n0}"的举报系统自动处理已完成，点击查看处理反馈':
        K + '{n0}"에 대한 신고가 시스템에서 자동 처리 완료되었습니다. 처리 결과를 보려면 클릭하세요.',
    D + '{n0}"的举报正在处理中.....点击查看举报详情和处理进度':
        K + '{n0}"에 대한 신고가 처리 중입니다..... 클릭하여 신고 상세 및 처리 진행 상황 보기',
    D2 + '{n0}"的': K2 + '{n0}"에 대한',
    D2 + '{n0}"的举报正在处理中.....点击查看举报详情和处理进度':
        K2 + '{n0}"에 대한 신고가 처리 중입니다..... 클릭하여 신고 상세 및 처리 진행 상황 보기',
    D2 + '{n0}"的举报系统自动处理已完成，点击查看处理反馈':
        K2 + '{n0}"에 대한 신고가 시스템에서 자동 처리 완료되었습니다. 처리 결과를 보려면 클릭하세요.',
}

FIX = {  # wrong or inconsistent translations
    "天械匹配": "랭크 매칭",
    "天梯模式炙热沙城Ⅱ [{0}:{1}]APRES": "랭크 모드 더스트2 [{0}:{1}]",
    "炙热沙城Ⅱ [{0}:{1}]": "더스트2 [{0}:{1}]",
    "远古造迹": "고대",
    "段位：B-精英A": "랭크 등급: B - 엘리트A",
    "段位：C - 精英B": "랭크 등급: C - 엘리트B",
    "段位：D- 精英C": "랭크 등급: D - 엘리트C",
    "理已完成，点击查看处理反馈{0}天前": "처리 완료, 클릭하여 처리 결과 보기 {0}일 전",
}

DROP = [  # sentences with names baked in, now covered by the templates above
    "westmelon(游戏中)", "Emiria正在天梯激战", "westmelon正在天梯激战", "s{0}mple正在天梯激战",
    "s{0}mple抖音直播", ";{0}mple抖音直播", "s{0}mple抖音直播天梯匹配",
    "道具作者：Tatukunn", "完美世界电竞APP昵称PW{0}",
    D + 'dOnik"的',
    D + '无限阿U跟单挑弼马温大闹墨西哥免费停车场"，"用户{4}"的举报正在处理中......点击查看举',
    D + '无限阿U跟单挑弼马温大闹墨西哥免费停车场"，"用户{4}"的举报系统自动处理已完成，点',
    D + '椰子马枪怪队友""害羞的榔子","{4}"的举报系统自动处理已完成，点击查看处理反馈',
    D + '清晨第{4}缕阳光","用户{5}"的举报正在处理',
    D + '清晨第{4}缕阳光"，"用户{5}"的举报系统自动处',
    # broken fragments (cut-off names, two panels read as one line)
    D + "清晨第{4}",
    "只业选手正在天梯激战le, westmelon正在进行",
    '返"，"蓝本新约”的举报正在处理中.....点击查',
    '返"，"蓝本新约的举报正在处理中....点击查',
    # v0.1.2 review
    D2 + 'Albert"的',
    D + '离不返"，"蓝本新约"的举报正在处理中.....点击查看举报详情和处理进度',
    '缕阳光"，"用户{0}"的举报正在处理', "离线（春序十言",
    "客服{0}丰川祥子", "帅哥本人{0}", "纯情猪头少年", "落叶~无回",
    "共克您达成成就【DFATH】{0}在CS的旅程", "妾{0}名挑战者的挑战。自定义模备好接受挑战者的枪林弹雨了",
    "这一行不在词典里",   # a test line that leaked into the cache
]


def main(paths):
    for zh in TEMPLATES:
        assert compile_template(zh), zh
    for path in map(Path, paths):
        cache = TranslationCache(path)
        before = len(cache)
        dropped = cache.remove([cache_key(z) for z in DROP])
        cache.put_many([(zh, ko, False, True) for zh, ko in TEMPLATES.items()], "migration")
        fixed = [(zh, ko, False) for zh, ko in FIX.items() if cache_key(zh) in cache.data]
        cache.put_many(fixed, "review")
        print(f"{path}: {before} -> {len(cache)} (dropped {dropped}, "
              f"{len(TEMPLATES)} templates, {len(fixed)} fixed)")


if __name__ == "__main__":
    main(sys.argv[1:])
