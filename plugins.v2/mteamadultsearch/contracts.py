"""Dependency-free M-Team request and MoviePilot download contracts."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Tuple


AV_KEYWORD_RE = re.compile(r"^[A-Z0-9]{2,12}(?:-[A-Z0-9]{1,12})*-[0-9]{2,9}$")
DEFAULT_API_BASE_URL = "https://api.m-team.cc/api"
MAX_KEYWORD_LENGTH = 100


# 常用简体字/异体字 -> 日文新字体/常用汉字映射，长尾未录入女优自动字形对齐
KANJI_SIMPLIFIED_TO_JAPANESE: Dict[str, str] = {
    "亚": "亜",
    "泽": "沢",
    "岛": "島",
    "滨": "浜",
    "樱": "桜",
    "结": "結",
    "关": "関",
    "荣": "栄",
    "艺": "芸",
    "条": "条",
    "绘": "絵",
    "实": "実",
    "步": "歩",
    "广": "広",
    "县": "県",
    "铃": "鈴",
    "绪": "緒",
    "纱": "紗",
    "仓": "倉",
    "庄": "荘",
    "莲": "蓮",
    "织": "織",
    "弥": "弥",
    "涩": "渋",
    "泷": "滝",
    "濑": "瀬",
    "齐": "斉",
    "斋": "斎",
    "丰": "豊",
    "龙": "竜",
    "鸟": "鳥",
    "黑": "黒",
    "静": "静",
    "惠": "恵",
    "爱": "愛",
    "桥": "橋",
    "枫": "楓",
    "浅": "浅",
    "藤": "藤",
    "叶": "葉",
    "长": "長",
    "馆": "館",
    "冈": "岡",
    "咲": "咲",
    "优": "優",
    "真": "真",
}

# 知名女优中文常用名/曾用名 -> 标准日文官方名及历史艺名别名映射
# 涵盖假名名字、改名女优、简繁差异显著的高频女优
ACTRESS_ALIASES: Dict[str, Tuple[str, ...]] = {
    # 顶级高频 / 顶流
    "三上悠亚": ("三上悠亜",),
    "相泽南": ("相沢みなみ",),
    "相泽美南": ("相沢みなみ",),
    "河北彩花": ("河北彩花", "河北彩伽"),
    "河北彩伽": ("河北彩伽", "河北彩花"),
    "深田咏美": ("深田えいみ", "天海こころ"),
    "深田永美": ("深田えいみ",),
    "天海心": ("天海こころ", "深田えいみ"),
    "波多野结衣": ("波多野結衣",),
    "桥本有菜": ("橋本ありな", "新ありな"),
    "新有菜": ("新ありな", "橋本ありな"),
    "枫可怜": ("楓カレン", "田中レモン"),
    "枫花恋": ("楓カレン", "田中レモン"),
    "田中柠檬": ("田中レモン", "楓カレン"),
    "铃村爱里": ("鈴村あいり",),
    "吉泽明步": ("吉沢明歩",),
    "明步": ("吉沢明歩",),
    "小野六花": ("小野六花",),
    "山岸逢花": ("山岸逢花", "山岸あいか"),
    "桃乃木香奈": ("桃乃木かな",),
    "桃乃木香菜": ("桃乃木かな",),
    "樱空桃": ("桜空もも",),
    "樱空桃子": ("桜空もも",),
    "伊藤舞雪": ("伊藤舞雪", "いとう舞雪"),
    "安斋拉拉": ("安斎らら", "RION", "宇都宮しおん"),
    "安斋萝拉": ("安斎らら", "RION", "宇都宮しおん"),
    "宇都宫紫苑": ("宇都宮しおん", "安斎らら"),
    "葵司": ("葵つかさ",),
    "白石茉莉奈": ("白石茉莉奈",),
    "高桥圣子": ("高橋しょう子", "高橋聖子"),
    "八挂海": ("八掛うみ",),
    "希岛爱理": ("希島あいり",),
    "水户香奈": ("水戸かな",),
    "七泽美亚": ("七沢みあ",),
    "七森莉莉": ("七ツ森りり",),
    "枢木葵": ("枢木あおい",),
    "仓多真央": ("倉多まお",),
    "唯井真寻": ("唯井まひろ",),
    "天使萌": ("天使もえ",),
    "凉森玲梦": ("涼森れむ",),
    "美谷朱里": ("美谷朱里",),
    "岬奈奈美": ("岬ななみ",),
    "永野一香": ("永野いち夏",),
    "雾岛樱": ("霧島さくら",),
    "莲实克蕾儿": ("蓮実クレア",),
    "纱仓真菜": ("紗倉まな",),
    "本庄铃": ("本庄鈴",),
    "筱田步美": ("篠田あゆみ",),
    "飞鸟铃": ("飛鳥りん",),
    "椎名空": ("椎名そら",),
    "根尾朱里": ("根尾あかり",),
    "希崎杰西卡": ("希崎ジェシカ",),
    "上原亚衣": ("上原亜衣",),
    "滨崎真绪": ("浜崎真緒",),
    "麻生希": ("麻生希",),
    "水野朝阳": ("水野朝陽",),
    "结城留美奈": ("結城るみな",),
    "松下纱荣子": ("松下紗栄子",),
    "辻井穗乃果": ("辻井ほのか",),
    "纯名桃香": ("純名もも",),
    "蕾": ("つぼみ",),
    "苍井空": ("蒼井そら",),
    "友田彩也香": ("友田彩也香",),
    "樱木优希音": ("桜木優希音",),
    "宫岛芽衣": ("宮島めい",),
    "小宵虎次": ("小宵こなつ",),
    "架乃由罗": ("架乃ゆら",),
    "石原希望": ("石原希望",),
    "楪可怜": ("楪カレン",),
    "逢见梨花": ("逢見リカ",),
    "栗山莉绪": ("栗山莉緒",),
    "希美真由": ("希美まゆ",),
    "梦乃爱华": ("夢乃あいか",),
    "浅田结梨": ("浅田結梨",),
    "坂道美琉": ("坂道みる", "miru"),
    "蜜美杏": ("蜜美杏",),
    "由爱可奈": ("由愛可奈",),
    "迹美朱莉": ("跡美しゅり",),
    "星奈爱": ("星奈あい",),
    "希志爱野": ("希志あいの",),
    "朝桐光": ("朝桐光",),
    "爱泽有纱": ("愛沢有紗",),
    "铃原爱蜜莉": ("鈴原エミリ",),
    "森泽佳奈": ("森沢かな", "飯岡かなこ"),
    "饭冈加奈子": ("飯岡かなこ", "森沢かな"),
    "初川南": ("初川みなみ",),
    "吉高宁宁": ("吉高寧々",),
    "金松季步": ("金松季歩",),
    "木下日葵": ("木下ひまり",),
    "渚恋生": ("渚恋生",),
    "樱萌子": ("桜もこ",),
    "斋藤亚美里": ("斎藤あみり",),
    "未步奈奈": ("未歩なな",),
    "枫芙爱": ("楓ふうあ",),
    "白桃朝日": ("白桃はな",),
    "浅野心": ("浅野こころ",),
    "小凑四叶": ("小湊よつば",),
    "明日花绮罗": ("明日花キララ",),
    "明日花": ("明日花キララ",),
    "大槻响": ("大槻ひびき",),
    "星川凛凛花": ("星川凛々花",),
    "户田真琴": ("戸田真琴",),
    "川上奈奈美": ("川上奈々美",),
    "篠田优": ("篠田ゆう",),
    "椎名由奈": ("椎名ゆな",),
    "川合明日香": ("川合あすか",),
    "望月彩香": ("望月あやか",),
    "星宫一花": ("星宮一花",),
    "逢泽真理亚": ("逢沢まりあ",),
    "仓木诗织": ("倉木しおり",),
    "铃木心春": ("鈴木心春",),
    "翼舞": ("つばさ舞",),
    "日向真凛": ("日向真凛",),
    "宝生莉莉": ("宝生リリー",),
    "佐山爱": ("佐山愛",),
    "宇流木罗拉": ("宇流木さらら",),
    "宝田萌奈美": ("宝田もなみ",),
    "冲田杏梨": ("沖田杏梨",),
    "羽咲美晴": ("羽咲みはる",),
    "三浦樱": ("三浦桜",),
    "美乃雀": ("美乃すずめ",),
    "八木奈奈": ("八木奈々",),
    "白峰美羽": ("白峰ミウ",),
    "广濑成美": ("広瀬なるみ",),
    "山井铃": ("山井すず",),
}


def normalize_av_keyword(value: str) -> str:
    """Normalize common AV-number separators without changing the identifier."""

    normalized = str(value or "").strip().upper()
    normalized = re.sub(r"[\s._]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized or not AV_KEYWORD_RE.fullmatch(normalized):
        raise ValueError("请输入有效的 AV 番号，例如 PRED-879")
    return normalized


def normalize_search_keyword(value: str) -> Tuple[str, bool]:
    """Return ``(keyword, is_av_number)`` for a search input.

    Strict AV numbers are normalized through :func:`normalize_av_keyword`.
    Any other non-empty input passes through as a free keyword (actor name
    and similar) with collapsed whitespace.
    """

    text = str(value or "").strip()
    if not text:
        raise ValueError("请输入搜索关键词，例如番号 PRED-879")
    compact = re.sub(r"[\s._]+", "-", text).upper()
    compact = re.sub(r"-+", "-", compact)
    if AV_KEYWORD_RE.fullmatch(compact):
        return compact, True
    free = re.sub(r"\s+", " ", text)
    if len(free) > MAX_KEYWORD_LENGTH:
        raise ValueError(f"搜索关键词过长（最多 {MAX_KEYWORD_LENGTH} 字符）")
    return free, False


def convert_simplified_to_japanese_kanji(text: str) -> str:
    """Convert common simplified Chinese characters to Japanese Shinjitai."""

    return "".join(KANJI_SIMPLIFIED_TO_JAPANESE.get(char, char) for char in text)


def expand_search_keywords(value: str) -> List[str]:
    """Expand a search keyword into a prioritized list of search terms.

    If the keyword is a strict AV number, it returns ``[normalized_code]``.
    For actress names or general keywords:
    1. If matched in :data:`ACTRESS_ALIASES`, prepends the canonical Japanese
       name and any known aliases/prior stage names.
    2. Runs single-character simplified-to-Japanese kanji conversion to bridge
       unlisted actress names (e.g. 亚->亜, 泽->沢).
    3. Retains the original input keyword as fallback.
    Duplicates are removed while preserving priority order.
    """

    keyword, is_av = normalize_search_keyword(value)
    if is_av:
        return [keyword]

    results: List[str] = []
    seen: set = set()

    def add(item: str) -> None:
        trimmed = item.strip()
        if trimmed and trimmed not in seen and len(trimmed) <= MAX_KEYWORD_LENGTH:
            seen.add(trimmed)
            results.append(trimmed)

    # 1. 精确匹配已知女优别名字典（去除空格后查表）
    clean_key = re.sub(r"\s+", "", keyword)
    matched_alias = False
    if clean_key in ACTRESS_ALIASES:
        matched_alias = True
        for alias in ACTRESS_ALIASES[clean_key]:
            add(alias)

    # 2. 简中转日文新字体字形对齐（保留原词空格结构）
    jp_converted = convert_simplified_to_japanese_kanji(keyword)
    clean_jp = re.sub(r"\s+", "", jp_converted)
    if clean_jp in ACTRESS_ALIASES and not matched_alias:
        matched_alias = True
        for alias in ACTRESS_ALIASES[clean_jp]:
            add(alias)

    if jp_converted != keyword:
        add(jp_converted)

    # 3. 原始用户输入词保底（未匹配别名时原词排在最前）
    add(keyword)

    return results


def build_search_payload(keyword: str, page: int, page_size: int) -> Dict[str, Any]:
    """Build the M-Team adult-mode request body."""

    normalized, _is_av_number = normalize_search_keyword(keyword)
    return {
        "keyword": normalized,
        "mode": "adult",
        "categories": [],
        "pageNumber": int(page),
        "pageSize": int(page_size),
        "visible": 1,
    }


def build_credential_enclosure(
    api_url: str,
    torrent_id: str,
    api_key: str,
    user_agent: str,
    use_proxy: bool,
) -> str:
    """Build MoviePilot's credential-style M-Team download URL."""

    request_descriptor = {
        "method": "post",
        "cookie": False,
        "params": {"id": str(torrent_id)},
        "header": {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "x-api-key": api_key,
        },
        "proxy": bool(use_proxy),
        "result": "data",
    }
    encoded = base64.b64encode(
        json.dumps(request_descriptor, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
    ).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrent/genDlToken"
