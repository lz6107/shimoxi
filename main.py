import os
import re
import json
import time
import html
import sqlite3
import hashlib
from datetime import datetime

import feedparser
import requests
from openai import OpenAI


# =========================
# 基础配置（石墨烯财经 新闻精选正式版）
# =========================

RSS_URLS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 正式频道：少发、精发、省 token
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))          # 每 30 分钟检查一次
SEND_DELAY = float(os.getenv("SEND_DELAY", "3"))
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "260"))   # 摘要缩短，省 token
MAX_FEED_ITEMS_PER_CHECK = int(os.getenv("MAX_FEED_ITEMS_PER_CHECK", "3"))

# 目标：每天 4-8 条新闻
MIN_NEWS_PER_DAY = int(os.getenv("MIN_NEWS_PER_DAY", "4"))
MAX_NEWS_PER_DAY = int(os.getenv("MAX_NEWS_PER_DAY", "8"))
MAX_NEWS_PER_CHECK = int(os.getenv("MAX_NEWS_PER_CHECK", "1"))

# 重要性门槛：白天严格，晚上没够 4 条时稍微放宽
MIN_IMPORTANCE_SCORE = int(os.getenv("MIN_IMPORTANCE_SCORE", "7"))
MIN_RELAXED_IMPORTANCE_SCORE = int(os.getenv("MIN_RELAXED_IMPORTANCE_SCORE", "5"))

MODEL_NAME = os.getenv("MODEL_NAME", "gpt-5.4-nano")
FIRST_RUN_SKIP_OLD = True
IMAGES_DIR = "images"

# 5 张图文件名
BTC_IMAGE = os.getenv("BTC_IMAGE", "btc.png")
ETH_IMAGE = os.getenv("ETH_IMAGE", "eth.png")
ALTCOIN_IMAGE = os.getenv("ALTCOIN_IMAGE", "altcoin.png")
ONCHAIN_IMAGE = os.getenv("ONCHAIN_IMAGE", "onchain.png")
MACRO_IMAGE = os.getenv("MACRO_IMAGE", "macro.png")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 低价值标题过滤
# =========================

SKIP_KEYWORDS = [
    "podcast",
    "newsletter",
    "video",
    "watch live",
    "live blog",
    "live updates",
    "opinion",
    "editorial",
    "interview",
    "sponsored",
    "press release",
    "partner",
    "partnership",
    "price prediction",
]


# =========================
# 重要性打分
# =========================

IMPORTANT_KEYWORDS = [
    # 主线资产
    ("bitcoin", 3), ("btc", 3),
    ("ethereum", 3), ("eth", 3),

    # ETF / 监管 / 宏观
    ("etf", 3), ("sec", 3), ("cftc", 3),
    ("fed", 3), ("federal reserve", 3),
    ("rate cut", 3), ("interest rate", 2),
    ("inflation", 2), ("cpi", 2),
    ("regulation", 2), ("lawsuit", 2), ("court", 2),

    # 交易所 / 机构
    ("binance", 2), ("coinbase", 2), ("okx", 2),
    ("blackrock", 2), ("fidelity", 2),
    ("microstrategy", 2), ("strategy", 1),

    # 链上 / 风险事件
    ("whale", 2), ("on-chain", 2), ("onchain", 2),
    ("inflow", 2), ("outflow", 2),
    ("liquidation", 3), ("liquidations", 3),
    ("hack", 4), ("hacked", 4), ("exploit", 4),
    ("stablecoin", 2), ("tether", 2), ("usdt", 2),
    ("circle", 2), ("usdc", 2),

    # 热门山寨
    ("solana", 2), ("sol", 2),
    ("xrp", 2), ("bnb", 2),
    ("dogecoin", 1), ("doge", 1),
    ("pepe", 1), ("meme", 1),
    ("ai token", 1), ("airdrop", 1),
]

LOW_VALUE_KEYWORDS = [
    "podcast",
    "newsletter",
    "video",
    "watch live",
    "live blog",
    "live updates",
    "opinion",
    "editorial",
    "interview",
    "sponsored",
    "press release",
    "partner",
    "partnership",
    "conference",
    "event recap",
]


def importance_score(title_en: str, summary_en: str = "") -> int:
    title_lower = (title_en or "").lower()
    text = f"{title_en} {summary_en}".lower()

    score = 0

    for kw, points in IMPORTANT_KEYWORDS:
        if kw in text:
            score += points

    for kw in LOW_VALUE_KEYWORDS:
        if kw in text:
            score -= 4

    # 核心词出现在标题里，额外加分
    hot_title_words = [
        "bitcoin", "btc", "ethereum", "eth",
        "etf", "sec", "fed",
        "binance", "coinbase",
        "hack", "exploit",
        "liquidation", "whale",
    ]

    for kw in hot_title_words:
        if kw in title_lower:
            score += 1

    # 明显大事件词，额外加分
    big_event_words = [
        "surges", "plunges", "falls", "jumps",
        "record", "approval", "approved",
        "rejects", "sues", "settlement",
        "breach", "stolen", "ban",
        "launches", "files", "warns",
    ]

    for kw in big_event_words:
        if kw in title_lower:
            score += 1

    return score


# =========================
# 数据库
# =========================

def init_db():
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_links (
            link TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_fingerprints (
            fingerprint TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    # 新增日志表：只用于统计和观察，不影响旧去重表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT,
            title TEXT,
            status TEXT,
            score INTEGER,
            reason TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def has_any_sent_data() -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sent_links")
    link_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sent_fingerprints")
    fp_count = cur.fetchone()[0]

    conn.close()
    return (link_count + fp_count) > 0


def has_sent_link(link: str) -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_links WHERE link = ?", (link,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def has_sent_fingerprint(fingerprint: str) -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_fingerprints WHERE fingerprint = ?", (fingerprint,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(link: str, fingerprint: str):
    now = datetime.now().isoformat()
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()

    if link:
        cur.execute(
            "INSERT OR IGNORE INTO sent_links(link, created_at) VALUES (?, ?)",
            (link, now)
        )

    if fingerprint:
        cur.execute(
            "INSERT OR IGNORE INTO sent_fingerprints(fingerprint, created_at) VALUES (?, ?)",
            (fingerprint, now)
        )

    conn.commit()
    conn.close()


def log_news(link: str, title: str, status: str, score: int = 0, reason: str = ""):
    """
    记录处理日志。
    为了避免 held 类新闻每 30 分钟刷一堆日志，同一个 link + status + reason 只记录一次。
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()

    cur.execute(
        """
        SELECT 1 FROM news_log
        WHERE link = ?
          AND status = ?
          AND reason = ?
        LIMIT 1
        """,
        (link, status, reason)
    )

    exists = cur.fetchone()

    if not exists:
        cur.execute(
            """
            INSERT INTO news_log(link, title, status, score, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (link, title, status, score, reason, now)
        )

    conn.commit()
    conn.close()


def sent_news_count_today() -> int:
    """
    统计今天已发送数量。

    兼容旧版本：
    - 新版本发送成功会写 news_log(status='sent')
    - 旧版本没有 news_log，只写 sent_links
    - 所以这里会把“今天 sent_links 里但 news_log 没记录的旧发送”也算上
    """
    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect("data.db")
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM news_log
        WHERE status = 'sent'
          AND created_at LIKE ?
        """,
        (today + "%",)
    )
    sent_from_log = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*)
        FROM sent_links sl
        WHERE sl.created_at LIKE ?
          AND NOT EXISTS (
              SELECT 1
              FROM news_log nl
              WHERE nl.link = sl.link
          )
        """,
        (today + "%",)
    )
    legacy_sent = cur.fetchone()[0]

    conn.close()
    return sent_from_log + legacy_sent


def current_min_importance_score() -> int:
    """
    白天严格筛选。
    如果到晚上还没到 4 条，稍微放宽，但不会低于 MIN_RELAXED_IMPORTANCE_SCORE。
    """
    sent_today = sent_news_count_today()
    hour = datetime.now().hour

    if sent_today >= MIN_NEWS_PER_DAY:
        return MIN_IMPORTANCE_SCORE

    if hour >= 22:
        return max(MIN_IMPORTANCE_SCORE - 2, MIN_RELAXED_IMPORTANCE_SCORE)

    if hour >= 18:
        return max(MIN_IMPORTANCE_SCORE - 1, MIN_RELAXED_IMPORTANCE_SCORE)

    return MIN_IMPORTANCE_SCORE


# =========================
# 文本处理
# =========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def shorten_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text

    cut = text[:max_len].rstrip()
    split_chars = ["。", "！", "？", "；", "，", ".", "!", "?", ";", ","]
    last_pos = -1
    for ch in split_chars:
        pos = cut.rfind(ch)
        if pos > last_pos:
            last_pos = pos
    if last_pos >= max_len // 2:
        cut = cut[:last_pos + 1].rstrip()
    return cut


def extract_summary(entry) -> str:
    raw_summary = (
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
    )

    content_list = getattr(entry, "content", None)
    if content_list and isinstance(content_list, list):
        for item in content_list:
            value = item.get("value", "")
            if value and len(value) > len(raw_summary):
                raw_summary = value

    summary_clean = clean_html(raw_summary)
    summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

    if len(summary_clean) < 40:
        return ""

    return shorten_text(summary_clean, MAX_SUMMARY_LENGTH)


def clean_one_line(text: str) -> str:
    if not text:
        return ""
    text = clean_html(text)
    text = text.replace("...", "").replace("……", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \n\r\t-—:：")


def clean_paragraph(text: str) -> str:
    if not text:
        return ""
    text = clean_html(text)
    text = text.replace("...", "").replace("……", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    return "\n".join(lines).strip()


def should_skip_title(title_en: str) -> bool:
    title_lower = (title_en or "").lower().strip()
    if not title_lower:
        return True
    return any(k in title_lower for k in SKIP_KEYWORDS)


def make_fingerprint(title_en: str) -> str:
    normalized = (title_en or "").lower()
    normalized = re.sub(r"&amp;", "and", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest() if normalized else ""


# =========================
# 图片处理（5图版）
# =========================

def image_path(filename: str) -> str:
    return os.path.join(IMAGES_DIR, filename)


def get_best_local_image(result: dict) -> str:
    image_type = result.get("image_type", "")

    mapping = {
        "btc": BTC_IMAGE,
        "eth": ETH_IMAGE,
        "altcoin": ALTCOIN_IMAGE,
        "onchain": ONCHAIN_IMAGE,
        "macro": MACRO_IMAGE,
    }

    filename = mapping.get(image_type, MACRO_IMAGE)
    path = image_path(filename)
    if os.path.isfile(path):
        return path

    fallback = image_path(MACRO_IMAGE)
    if os.path.isfile(fallback):
        return fallback

    return ""


# =========================
# AI 提示词
# =========================

SYSTEM_PROMPT = """
你是“石墨烯财经”的中文加密市场编辑，负责把英文加密新闻加工成适合中文频道发布的内容。

覆盖主题：
比特币、以太坊、山寨币、链上趋势、宏观与加密

你的任务不是机械翻译，而是做中文编译和市场提炼。

要求：
1. 不要逐句直译，不要翻译腔
2. 不要输出英文
3. 不要输出原新闻标题、原新闻摘要、来源、链接
4. title_cn 要写成简洁、有判断、有内容感的中文短标题，不能太长，建议 8 到 16 个字
5. main_text 要写成适合频道发布的正文，2到4句
6. takeaway 只写1句，作为最后的“一句话”判断
7. 同时判断 image_type、bias
8. 语言自然、简洁、专业，不要喊单，不要夸张
9. 不要保留原新闻痕迹，要像重新加工后的中文内容
10. 不要总是使用同一种句式开头
11. 只输出 JSON，不要输出 JSON 以外的任何内容

image_type 只能是：
btc、eth、altcoin、onchain、macro

bias 只能是：
偏多、偏空、中性、观望
""".strip()


def build_user_prompt(title_en: str, summary_en: str) -> str:
    return f"""
请根据下面这条英文加密新闻，输出一个 JSON 对象，不要输出 JSON 以外的任何内容。

JSON 格式必须严格如下：
{{
  "title_cn": "简洁中文标题",
  "image_type": "btc/eth/altcoin/onchain/macro",
  "bias": "偏多/偏空/中性/观望",
  "main_text": "2到4句加工后的中文正文",
  "takeaway": "1句简短核心判断"
}}

字段要求：
1. title_cn：简洁自然，有内容感，不要太长，建议 8 到 16 个字，不要写成营销标题党
2. image_type 只能是：btc、eth、altcoin、onchain、macro
3. bias 只能是：偏多、偏空、中性、观望
4. main_text：写成自然中文资讯风格，2到4句，不要翻译腔，不要来源痕迹
5. takeaway：只写一句话，适合作为“ 一句话：xxx ”
6. 不要输出英文
7. 不要输出来源
8. 不要输出链接
9. 不要输出多余字段
10. 不要使用省略号
11. 句子必须完整

image_type 参考规则：
- btc：比特币、BTC、比特币ETF、矿工、比特币主导行情
- eth：以太坊、ETH、L2、以太坊生态明显相关
- altcoin：SOL、XRP、DOGE、BNB、MEME、公链、山寨币轮动
- onchain：链上数据、地址、资金流向、巨鲸、质押、解锁、链上趋势
- macro：监管、政策、SEC、ETF审批、宏观、利率、全球市场、综合快讯

英文标题：
{title_en}

英文摘要：
{summary_en if summary_en else "（无摘要）"}
""".strip()


def extract_json_object(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0).strip() if m else ""


def ai_compile_news(title_en: str, summary_en: str) -> dict:
    prompt = build_user_prompt(title_en, summary_en)

    response = client.responses.create(
        model=MODEL_NAME,
        instructions=SYSTEM_PROMPT,
        input=prompt,
    )

    raw_text = (response.output_text or "").strip()
    raw_json = extract_json_object(raw_text)
    if not raw_json:
        return {}

    try:
        data = json.loads(raw_json)
    except Exception:
        return {}

    title_cn = clean_one_line(str(data.get("title_cn", "")))
    image_type = clean_one_line(str(data.get("image_type", "")))
    bias = clean_one_line(str(data.get("bias", "")))
    main_text = clean_paragraph(str(data.get("main_text", "")))
    takeaway = clean_one_line(str(data.get("takeaway", "")))

    valid_types = {"btc", "eth", "altcoin", "onchain", "macro"}
    valid_bias = {"偏多", "偏空", "中性", "观望"}

    if image_type not in valid_types:
        return {}
    if bias not in valid_bias:
        return {}
    if not title_cn or not main_text or not takeaway:
        return {}

    return {
        "title_cn": title_cn,
        "image_type": image_type,
        "bias": bias,
        "main_text": main_text,
        "takeaway": takeaway,
    }


# =========================
# 标签映射
# =========================

PRIMARY_TAG_MAP = {
    "btc": "#BTC",
    "eth": "#ETH",
    "altcoin": "#山寨币",
    "onchain": "#链上",
    "macro": "#宏观",
}

SECONDARY_TAG_MAP = {
    "btc": "#加密市场",
    "eth": "#加密市场",
    "altcoin": "#加密市场",
    "onchain": "#链上观察",
    "macro": "#政策解读",
}


def build_final_text(result: dict) -> str:
    primary_tag = PRIMARY_TAG_MAP[result["image_type"]]
    secondary_tag = SECONDARY_TAG_MAP[result["image_type"]]
    bias_tag = "#" + result["bias"]

    return f"""石墨烯财经｜{result["title_cn"]}

{result["main_text"]}

一句话：{result["takeaway"]}
{primary_tag} {secondary_tag} {bias_tag}""".strip()


# =========================
# Telegram 发送
# =========================

def safe_caption(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= 1024:
        return text
    return text[:1000].rstrip() + "\n……"


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=30
    )
    print("sendMessage 结果:", resp.status_code, resp.text)
    return resp


def send_telegram_photo_by_file(photo_path: str, caption: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": safe_caption(caption)
            },
            files={"photo": f},
            timeout=30
        )
    print("sendPhoto(file) 结果:", resp.status_code, resp.text)
    return resp


# =========================
# 候选新闻收集
# =========================

def collect_candidates_from_feed(feed_url: str, seen_fingerprints: set) -> list:
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")

    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return []

    entries = list(feed.entries[:MAX_FEED_ITEMS_PER_CHECK])
    entries.reverse()

    first_run = not has_any_sent_data()
    min_score = current_min_importance_score()

    candidates = []

    print(
        f"今日已发 {sent_news_count_today()} 条，"
        f"当前最低分 {min_score}，"
        f"本源候选 {len(entries)} 条"
    )

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())
        fingerprint = make_fingerprint(title_en)

        if not link or not title_en:
            continue

        if fingerprint and fingerprint in seen_fingerprints:
            print("本轮重复标题，跳过:", title_en)
            continue

        if should_skip_title(title_en):
            print("跳过低价值标题:", title_en)
            mark_sent(link, fingerprint)
            log_news(link, title_en, "skipped", 0, "low_value_title")
            continue

        if has_sent_link(link) or (fingerprint and has_sent_fingerprint(fingerprint)):
            print("已存在，跳过:", title_en)
            continue

        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link, fingerprint)
            log_news(link, title_en, "skipped", 0, "first_run_skip_old")
            continue

        summary_en = extract_summary(entry)
        summary_en = shorten_text(summary_en, MAX_SUMMARY_LENGTH)

        score = importance_score(title_en, summary_en)

        # 极低分新闻，永久跳过，避免以后反复处理
        if score < MIN_RELAXED_IMPORTANCE_SCORE:
            print(
                f"重要性太低，永久跳过，不调用AI: "
                f"score={score}, min_relaxed={MIN_RELAXED_IMPORTANCE_SCORE}, title={title_en}"
            )
            mark_sent(link, fingerprint)
            log_news(link, title_en, "skipped", score, "very_low_importance")
            continue

        # 中等新闻，白天先不发，晚上如果还没够数量，可能放宽后再发
        if score < min_score:
            print(
                f"重要性不足，暂缓，不调用AI: "
                f"score={score}, min={min_score}, title={title_en}"
            )
            log_news(link, title_en, "held", score, "below_current_threshold")
            continue

        candidates.append({
            "feed_url": feed_url,
            "link": link,
            "title_en": title_en,
            "summary_en": summary_en,
            "fingerprint": fingerprint,
            "score": score,
        })

        if fingerprint:
            seen_fingerprints.add(fingerprint)

    return candidates


def collect_all_candidates() -> list:
    all_candidates = []
    seen_fingerprints = set()

    for rss in RSS_URLS:
        try:
            candidates = collect_candidates_from_feed(rss, seen_fingerprints)
            all_candidates.extend(candidates)
        except Exception as e:
            print(f"处理 RSS 失败 {rss}: {e}")

    # 分数高的优先；同分时保持 RSS 抓取顺序
    all_candidates.sort(key=lambda x: x["score"], reverse=True)

    if all_candidates:
        print("本轮高分候选：")
        for c in all_candidates[:5]:
            print(f"score={c['score']} | {c['title_en']}")
    else:
        print("本轮没有达到发送门槛的候选新闻")

    return all_candidates


# =========================
# 发送候选新闻
# =========================

def process_candidate(candidate: dict):
    link = candidate["link"]
    title_en = candidate["title_en"]
    summary_en = candidate["summary_en"]
    fingerprint = candidate["fingerprint"]
    score = candidate["score"]

    print(f"进入AI处理: score={score}, title={title_en}")

    result = ai_compile_news(title_en, summary_en)
    if not result:
        print("AI 结果无效，跳过:", title_en)
        mark_sent(link, fingerprint)
        log_news(link, title_en, "skipped", score, "invalid_ai_result")
        return False

    final_text = build_final_text(result)
    photo_path = get_best_local_image(result)

    if photo_path and os.path.isfile(photo_path):
        resp = send_telegram_photo_by_file(photo_path, final_text)
        if resp.status_code != 200:
            print("图片发送失败，改为纯文字")
            resp = send_telegram_message(final_text)
    else:
        resp = send_telegram_message(final_text)

    if resp.status_code == 200:
        mark_sent(link, fingerprint)
        log_news(link, title_en, "sent", score, "sent_ok")
        print(f"已发送: score={score}, title={title_en}")
        return True

    print("发送失败，未记录为已发送:", title_en)
    log_news(link, title_en, "failed", score, f"telegram_status_{resp.status_code}")
    return False


def process_one_check() -> int:
    if sent_news_count_today() >= MAX_NEWS_PER_DAY:
        print(f"今日新闻已达到上限 {MAX_NEWS_PER_DAY} 条，本轮不再处理")
        return 0

    candidates = collect_all_candidates()

    if not candidates:
        return 0

    sent_this_check = 0

    for candidate in candidates:
        if sent_news_count_today() >= MAX_NEWS_PER_DAY:
            print(f"今日已达到 {MAX_NEWS_PER_DAY} 条上限，停止发送")
            break

        if sent_this_check >= MAX_NEWS_PER_CHECK:
            print(f"本轮已达到发送上限 {MAX_NEWS_PER_CHECK} 条")
            break

        try:
            ok = process_candidate(candidate)
            if ok:
                sent_this_check += 1
        except Exception as e:
            print("处理失败:", candidate["title_en"], "->", e)
            log_news(
                candidate["link"],
                candidate["title_en"],
                "failed",
                candidate["score"],
                str(e)[:200]
            )

        time.sleep(SEND_DELAY)

    return sent_this_check


# =========================
# 主流程
# =========================

def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")
    if not OPENAI_API_KEY:
        raise ValueError("缺少环境变量 OPENAI_API_KEY")

    init_db()

    print("石墨烯财经新闻精选机器人启动成功（正式省 token 版）")
    print("频道:", CHAT_ID)
    print("检查间隔:", CHECK_INTERVAL)
    print("每天目标:", f"{MIN_NEWS_PER_DAY}-{MAX_NEWS_PER_DAY} 条")
    print("每轮最多发送:", MAX_NEWS_PER_CHECK)
    print("白天最低分:", MIN_IMPORTANCE_SCORE)
    print("放宽最低分:", MIN_RELAXED_IMPORTANCE_SCORE)

    while True:
        try:
            sent_this_check = process_one_check()
        except Exception as e:
            print("本轮处理失败:", e)
            sent_this_check = 0

        print(
            f"本轮已发送 {sent_this_check} 条，"
            f"今日已发送 {sent_news_count_today()} / {MAX_NEWS_PER_DAY} 条，"
            f"休眠 {CHECK_INTERVAL} 秒...\n"
        )

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
