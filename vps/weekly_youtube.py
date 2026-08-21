#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily-trend-radar: 毎週土曜9:00(JST)にYouTubeで伸びとる動画を拾ってLINEに配信する。

読み手は美容室経営コンサルタントで、自分でInstagramリールを作っとる日本語話者。
「リール 編集 やり方」など指定キーワードで直近30日公開・再生数10,000回以上の動画を検索する。
`relevanceLanguage=ja` はYouTube APIでは「ヒント」程度にしか効かんので、取得後に
タイトルのひらがな/カタカナの有無で自前判定して日本語動画を優先する
(漢字は中国語動画にも含まれるため判定条件に使わない)。
関連語を含まん動画・除外語(レシピ/ゲーム実況等)を含む動画は落とす。
最終的に日本語動画を上・英語動画を下(最大2本、タイトル先頭に《英》を付与)に並べ、
上位5本をLINEに送る(同一チャンネル最大2本まで)。

流儀はdeliver.pyに合わせている(このファイルはdeliver.pyをimportせず独立して動く。
共通処理は意図的にコピーしている):
- ログはJSTタイムスタンプ、ファイル+標準エラーの二重出力。
- 設定ファイルは/etc/daily-trend-radar/config.env(deliver.pyと共用)からKEY=VALUEで読む。
- LINE Messaging APIのpushはdeliver.pyと同じエンドポイント・payload形。
- 状態ファイルに実行日を記録し、同じ日の二重実行を防ぐ。
- `--dry-run` を付けて実行すると、LINE送信・状態ファイル更新を一切行わず、
  組み上げるはずだった本文を標準出力に表示するだけで終わる(deliver.pyと同じ穴)。

deliver.pyとの違い:
- healthchecks.ioへのpingは行わない(週1実行のため専用の死活監視を用意していない)。
  その代わり、異常終了は必ず非ゼロ終了させてログに理由を残す。
- YouTube Data API v3の`search.list`は1日100回の専用クォータを消費する。
  クォータ超過(HTTP 403 / quotaExceeded)はネットワークエラーと区別してログに明記する。
  リトライはしない(無駄にクォータを消費するだけのため)。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MESSAGE_LIMIT = 5000

DEFAULT_CONFIG_PATH = "/etc/daily-trend-radar/config.env"
DEFAULT_LOG_PATH = "/var/log/daily-trend-radar-youtube.log"
DEFAULT_STATE_FILE = "/var/lib/daily-trend-radar/last_youtube.txt"

FETCH_TIMEOUT_SEC = 15
LINE_TIMEOUT_SEC = 30

SEARCH_MAX_RESULTS = 10
VIDEOS_CHUNK_SIZE = 50  # videos.listは1回のリクエストでidを最大50件まとめて取れる
MAX_PER_CHANNEL = 2  # 同一チャンネルからは最大2本まで(多様性の確保)
TOP_N = 5
DAYS_BACK = 30  # 検索対象期間(公開からの日数)。7日だと伸びきる前の動画ばかり拾ってしまうため30日に広げた
MIN_VIEWS = 10000  # これ未満は「伸びとる」と呼べんため除外
MAX_ENGLISH = 2  # 英語動画は最大この本数まで(残りは日本語で埋める)

# 後から足しやすいよう定数として持つ。読み手=美容室経営コンサルタントで自分でリールを作る人。
KEYWORDS = [
    "リール 編集 やり方",
    "ショート動画 冒頭 フック",
    "動画編集 テロップ 入れ方",
    "インスタ リール 伸ばす 方法",
    "美容室 SNS 集客",
]

# タイトル or 説明文にこの語が1つも含まれん動画は無関係とみなして落とす(大小文字は区別しない)
RELATED_WORDS = [
    "編集", "テロップ", "字幕", "リール", "ショート", "フック", "冒頭",
    "撮影", "カット", "演出", "動画", "サムネ", "バズ", "伸ば", "再生数",
    "SNS", "Instagram", "インスタ", "TikTok", "YouTube", "Reels", "Shorts",
    "editing", "edit", "hook", "retention", "thumbnail", "viral video",
]

# タイトルにこの語が含まれとったら無関係として落とす(大小文字は区別しない)
EXCLUDE_WORDS = [
    "レシピ", "recipe", "料理", "cooking", "cake", "food", "먹방",
    "ゲーム実況", "gameplay", "ASMR", "MV", "Official Video", "歌ってみた",
    "vlog", "ルーティン", "購入品",
]

# YouTube APIのクォータエラーとして扱うreason(新旧両対応)
QUOTA_ERROR_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class QuotaExceededError(RuntimeError):
    """YouTube APIのクォータ超過を明示するための例外(ネットワークエラーと区別する)。"""


class JSTFormatter(logging.Formatter):
    """ログのタイムスタンプを常にJSTで出す(サーバーのTZ設定に依存しない)。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=JST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")


def setup_logging() -> None:
    # WEEKLY_YOUTUBE_LOG はローカル検証用の上書き口。本番はデフォルトのまま使う。
    log_path = os.environ.get("WEEKLY_YOUTUBE_LOG", DEFAULT_LOG_PATH)
    formatter = JSTFormatter(fmt="%(asctime)s [%(levelname)s] %(message)s")

    handlers: list[logging.Handler] = []
    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as e:
        print(f"WARNING: ログファイルに書けん({log_path}): {e}", file=sys.stderr)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        root.addHandler(h)


def parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            values[key] = val
    return values


def load_config() -> dict[str, str]:
    # DAILY_TREND_RADAR_CONFIG はdeliver.pyと共通のローカル検証用上書き口(同じ設定ファイルのため)。
    config_path = os.environ.get("DAILY_TREND_RADAR_CONFIG", DEFAULT_CONFIG_PATH)
    file_values: dict[str, str] = {}
    if os.path.isfile(config_path):
        file_values = parse_env_file(config_path)
    else:
        logging.warning("設定ファイルが見つからん: %s (環境変数のみで動作)", config_path)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_values.get(key) or default

    config = {
        "YOUTUBE_API_KEY": get("YOUTUBE_API_KEY"),
        "LINE_CHANNEL_ACCESS_TOKEN": get("LINE_CHANNEL_ACCESS_TOKEN"),
        "LINE_USER_ID": get("LINE_USER_ID"),
        "YOUTUBE_STATE_FILE": get("YOUTUBE_STATE_FILE", DEFAULT_STATE_FILE),
    }

    missing = [
        k for k in ("YOUTUBE_API_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID") if not config[k]
    ]
    if missing:
        raise RuntimeError(f"必須設定が不足しとる: {', '.join(missing)}")

    return config  # type: ignore[return-value]


def read_state(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content or None
    except FileNotFoundError:
        return None


def write_state(path: str, date_str: str) -> None:
    state_dir = os.path.dirname(path)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(date_str)
    os.replace(tmp_path, path)


def _extract_error_reason(body: bytes) -> str | None:
    """YouTube APIのエラーレスポンス本文からerrors[0].reasonを取り出す(取れなければNone)。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    errors = (data.get("error") or {}).get("errors") or []
    if errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return None


def _http_get_json(url: str, params: dict, timeout: int) -> dict:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(
        full_url,
        method="GET",
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "daily-trend-radar-weekly-youtube/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        reason = _extract_error_reason(body)
        body_text = body.decode("utf-8", "replace")
        if e.code == 403 and reason in QUOTA_ERROR_REASONS:
            raise QuotaExceededError(
                f"YouTube APIクォータ超過(HTTP 403 reason={reason}): {url}"
            ) from e
        raise RuntimeError(f"YouTube API失敗(HTTP {e.code}): {url}: {body_text[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"YouTube API失敗(ネットワークエラー): {url}: {e}") from e

    if status != 200:
        raise RuntimeError(f"YouTube API失敗(HTTP {status}): {url}")

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"YouTube APIレスポンスのパースに失敗: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError("YouTube APIレスポンスの形式が想定外(dictでない)")

    return data


def search_video_ids(api_key: str, keyword: str, published_after: str) -> list[str]:
    params = {
        "part": "snippet",
        "type": "video",
        "order": "viewCount",
        "publishedAfter": published_after,
        "regionCode": "JP",
        "relevanceLanguage": "ja",
        "maxResults": SEARCH_MAX_RESULTS,
        "q": keyword,
        "key": api_key,
    }
    data = _http_get_json(YOUTUBE_SEARCH_URL, params, FETCH_TIMEOUT_SEC)
    items = data.get("items") or []
    video_ids: list[str] = []
    for item in items:
        video_id = (item.get("id") or {}).get("videoId")
        if video_id:
            video_ids.append(video_id)
    return video_ids


# ひらがな/カタカナのUnicode範囲。漢字(一-鿿)は中国語動画にも含まれるため
# 日本語判定には使わない(ひらがな or カタカナが1文字でもあれば日本語とみなす)。
_HIRAGANA_RANGE = (0x3040, 0x309F)
_KATAKANA_RANGE = (0x30A0, 0x30FF)


def is_japanese_title(title: str) -> bool:
    """タイトルにひらがな or カタカナが1文字でも含まれとれば日本語動画とみなす。"""
    for ch in title:
        code = ord(ch)
        if _HIRAGANA_RANGE[0] <= code <= _HIRAGANA_RANGE[1]:
            return True
        if _KATAKANA_RANGE[0] <= code <= _KATAKANA_RANGE[1]:
            return True
    return False


def is_related(title: str, description: str) -> bool:
    """タイトル or 説明文にRELATED_WORDSが1つでも含まれとれば関連ありとみなす(大小文字区別なし)。"""
    haystack = f"{title}\n{description}".lower()
    return any(word.lower() in haystack for word in RELATED_WORDS)


def is_excluded(title: str) -> bool:
    """タイトルにEXCLUDE_WORDSが1つでも含まれとれば無関係として除外する(大小文字区別なし)。"""
    haystack = title.lower()
    return any(word.lower() in haystack for word in EXCLUDE_WORDS)


def parse_video_item(item: dict) -> dict | None:
    video_id = item.get("id")
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    if not video_id or not snippet:
        return None
    try:
        view_count = int(statistics.get("viewCount", 0))
    except (TypeError, ValueError):
        view_count = 0
    try:
        like_count = int(statistics.get("likeCount", 0))
    except (TypeError, ValueError):
        like_count = 0
    title = (snippet.get("title") or "").strip()
    return {
        "video_id": video_id,
        "title": title,
        "description": (snippet.get("description") or "").strip(),
        "channel_id": snippet.get("channelId") or "",
        "channel_title": (snippet.get("channelTitle") or "").strip(),
        "view_count": view_count,
        "like_count": like_count,
        "url": f"https://youtu.be/{video_id}",
        "is_japanese": is_japanese_title(title),
    }


def fetch_video_details(api_key: str, video_ids: list[str]) -> list[dict]:
    videos: list[dict] = []
    for i in range(0, len(video_ids), VIDEOS_CHUNK_SIZE):
        chunk = video_ids[i : i + VIDEOS_CHUNK_SIZE]
        params = {
            "part": "statistics,snippet",
            "id": ",".join(chunk),
            "key": api_key,
        }
        data = _http_get_json(YOUTUBE_VIDEOS_URL, params, FETCH_TIMEOUT_SEC)
        for item in data.get("items") or []:
            video = parse_video_item(item)
            if video:
                videos.append(video)
    return videos


def filter_videos(videos: list[dict], min_views: int = MIN_VIEWS) -> list[dict]:
    """再生数不足・除外語ヒット・関連語なし、のいずれかに当てはまる動画を落とす。"""
    filtered: list[dict] = []
    for v in videos:
        if v.get("view_count", 0) < min_views:
            continue
        title = v.get("title") or ""
        description = v.get("description") or ""
        if is_excluded(title):
            continue
        if not is_related(title, description):
            continue
        filtered.append(v)
    return filtered


def rank_videos(
    videos: list[dict],
    max_per_channel: int = MAX_PER_CHANNEL,
    top_n: int = TOP_N,
    max_english: int = MAX_ENGLISH,
) -> list[dict]:
    """日本語動画を優先し、英語動画は最大max_english本だけ末尾に添えてtop_n本を返す。

    日本語・英語それぞれ再生数の多い順に並べ、同一チャンネルはmax_per_channel本まで
    (日本語・英語を通じてカウント)。日本語だけでtop_n本埋まれば英語は0本になる。
    """
    ja_sorted = sorted(
        (v for v in videos if v.get("is_japanese")), key=lambda v: v.get("view_count", 0), reverse=True
    )
    en_sorted = sorted(
        (v for v in videos if not v.get("is_japanese")), key=lambda v: v.get("view_count", 0), reverse=True
    )

    channel_counts: dict[str, int] = {}
    selected: list[dict] = []

    def take(candidates: list[dict], limit: int) -> None:
        for v in candidates:
            if len(selected) >= top_n or len(selected) >= limit:
                return
            channel_id = v.get("channel_id") or v.get("channel_title") or ""
            if channel_counts.get(channel_id, 0) >= max_per_channel:
                continue
            selected.append(v)
            channel_counts[channel_id] = channel_counts.get(channel_id, 0) + 1

    take(ja_sorted, top_n)

    remaining_slots = top_n - len(selected)
    english_quota = min(max_english, remaining_slots)
    if english_quota > 0:
        take(en_sorted, len(selected) + english_quota)

    return selected


def format_view_count(n: int) -> str:
    if n >= 10000:
        man = n / 10000
        if man >= 100:
            return f"{man:.0f}万回"
        return f"{man:.1f}万回"
    return f"{n}回"


def build_message(videos: list[dict], today_label: str) -> str:
    header = f"\U0001F4C8 今週伸びとる動画（{today_label}）"

    # 5000文字に収まるまで末尾から間引く。
    keep = len(videos)
    while True:
        lines = [header, ""]
        for i, v in enumerate(videos[:keep], start=1):
            title = (v.get("title") or "").strip()
            if not v.get("is_japanese"):
                title = f"《英》{title}"
            channel_title = (v.get("channel_title") or "").strip()
            view_label = format_view_count(v.get("view_count", 0))
            url = (v.get("url") or "").strip()
            lines.append(f"{i}. {title}")
            lines.append(f"   {channel_title} ／ {view_label}")
            lines.append(f"   {url}")
            lines.append("")
        message = "\n".join(lines).rstrip("\n")
        if len(message) <= LINE_MESSAGE_LIMIT or keep <= 0:
            return message
        keep -= 1


def send_line(token: str, user_id: str, message: str) -> int:
    payload = json.dumps(
        {"to": user_id, "messages": [{"type": "text", "text": message[:LINE_MESSAGE_LIMIT]}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        LINE_PUSH_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LINE_TIMEOUT_SEC) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        logging.error("LINE push失敗(HTTP %s): %s", e.code, body[:500])
        return e.code
    except urllib.error.URLError as e:
        # ネットワークエラーはここで握りつぶさず上に投げて非ゼロ終了させる
        raise RuntimeError(f"LINE push失敗(ネットワークエラー): {e}") from e


def main() -> int:
    setup_logging()
    dry_run = "--dry-run" in sys.argv[1:]
    logging.info("weekly_youtube.py 開始%s", "(dry-run)" if dry_run else "")

    try:
        config = load_config()
        state_path = config["YOUTUBE_STATE_FILE"]
        today = datetime.now(JST)
        today_key = today.strftime("%Y-%m-%d")
        last_run = read_state(state_path)

        if last_run == today_key:
            logging.info("実行スキップ(本日は実行済み): date=%s", today_key)
            logging.info("weekly_youtube.py 正常終了%s", "(dry-run)" if dry_run else "")
            return 0

        api_key = config["YOUTUBE_API_KEY"]
        published_after = (
            (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK))
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        video_ids: dict[str, None] = {}  # 挿入順を保った重複排除
        for keyword in KEYWORDS:
            ids = search_video_ids(api_key, keyword, published_after)
            logging.info("search.list 完了: keyword=%s hits=%s", keyword, len(ids))
            for video_id in ids:
                video_ids.setdefault(video_id, None)

        if not video_ids:
            logging.info("該当動画なし(検索結果0件)")
            ranked: list[dict] = []
        else:
            details = fetch_video_details(api_key, list(video_ids.keys()))
            logging.info("videos.list 完了: 詳細取得件数=%s", len(details))
            filtered = filter_videos(details)
            logging.info(
                "フィルタ完了: 再生数%s回以上・関連語あり・除外語なし=%s件(取得%s件中)",
                MIN_VIEWS, len(filtered), len(details),
            )
            ranked = rank_videos(filtered)

        if ranked:
            today_label = f"{today.month}/{today.day}"
            message = build_message(ranked, today_label)
            ja_count = sum(1 for v in ranked if v.get("is_japanese"))
            en_count = len(ranked) - ja_count
            if dry_run:
                logging.info(
                    "[dry-run] LINE送信をスキップし、本文を標準出力に表示する: 件数=%s(日本語%s/英語%s)",
                    len(ranked), ja_count, en_count,
                )
                print(message)
            else:
                status = send_line(config["LINE_CHANNEL_ACCESS_TOKEN"], config["LINE_USER_ID"], message)
                if status != 200:
                    # ここでraiseして状態ファイル更新前に非ゼロ終了させる
                    raise RuntimeError(f"LINE push失敗のため中断(http={status})")
                logging.info(
                    "LINE配信完了: 件数=%s(日本語%s/英語%s) http=%s 文字数=%s",
                    len(ranked), ja_count, en_count, status, len(message),
                )
        else:
            logging.info("配信対象なし、LINE送信スキップ")

        if dry_run:
            logging.info("[dry-run] state更新をスキップ")
        else:
            write_state(state_path, today_key)
        logging.info("weekly_youtube.py 正常終了%s", "(dry-run)" if dry_run else "")
        return 0

    except QuotaExceededError as e:
        logging.error("YouTube APIクォータ超過のため中断(リトライはしない): %s", e)
        return 1
    except Exception:
        logging.exception("処理中に異常終了")
        return 1


if __name__ == "__main__":
    sys.exit(main())
