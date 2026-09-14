#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily-trend-radar: 毎日(JST)にYouTubeで「AIの手口(使い方・やり方)」動画を拾ってLINEに配信する。

読み手は美容室経営コンサルタントで、Claude Code / ChatGPT / Notion / LINE Bot / VPS運用を
日常的に自分で触る実務家(daily-trend-radar/PROMPT.mdの「読む人」節と同一人物)。
「ChatGPT 使い方 業務効率化」など指定キーワードで直近30日公開・再生数10,000回以上の動画を検索する。

★このスクリプトは`weekly_youtube.py`(既存・毎週土曜9:00・動画編集/SNS集客ジャンル)とは
**完全に独立した別ジョブ**として作った。既存ジョブの設定ファイル・状態ファイル・アーカイブ先を
1つも共有しない(共有すると「本日実行済み」判定やアーカイブの日付ファイルが衝突して
既存ジョブを壊しかねないため)。KEYWORDS/RELATED_WORDS/EXCLUDE_WORDSも既存ジョブと語彙が
被らないよう別セットにしている(依頼元の方針：ニュースではなく手口だけを拾う。詳細は
`news-triage/_youtube-daily-実装メモ-2026-09-14.md`のキーワード選定理由を参照)。

deliver.py/weekly_youtube.pyと共通の流儀:
- ログはJSTタイムスタンプ、ファイル+標準エラーの二重出力。
- 設定ファイルは/etc/daily-trend-radar/config.env(deliver.py/weekly_youtube.pyと共用。
  YOUTUBE_API_KEYとLINE系の値は既存のものをそのまま使い回す。新規追加値は無い)。
- LINE Messaging APIのpushはdeliver.py/weekly_youtube.pyと同じエンドポイント・payload形。
- 状態ファイルに実行日を記録し、同じ日の二重実行を防ぐ(ただし状態ファイルは専用の別パス)。
- `--dry-run` を付けて実行すると、LINE送信・状態ファイル更新・seen追記を一切行わず、
  組み上げるはずだった本文を標準出力に表示するだけで終わる(既存2本と同じ穴)。

既存2本との違い:
- **毎日実行**が前提のため、「同じ動画が何日も出続ける」問題への対策として、一度選んで
  送った動画のvideo_idを恒久的に記録するseenファイル(YOUTUBE_AI_SEEN_FILE)を持つ。
  フィルタ後の候補からseen済みのvideo_idを除外してからランキングする(この一手間は
  週1実行のweekly_youtube.pyには無い。★これは依頼書に明記されていない設計判断であり、
  実装メモに「社長の判断が要る論点」として明記している)。
- YouTube Data API v3の`search.list`は1日100回の専用クォータを消費する。5キーワード分＝
  1日5回の消費であり、weekly_youtube.pyの週1回5消費と合わせても十分余裕がある
  (詳細計算はnews-triageの調査レポート参照)。
- healthchecks.ioへのping・本文の恒久保存(archive)は両方とも実装している(weekly_youtube.py
  の2026-08-22改修と同じ設計をコピー)。ただしconfig/state/archiveの実体パスは全部専用の
  別ファイル・別ディレクトリ(冒頭の独立性の説明を参照)。
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
# ★既存2本(daily-trend-radar.log / daily-trend-radar-youtube.log)と衝突せん専用パス。
DEFAULT_LOG_PATH = "/var/log/daily-trend-radar-youtube-ai.log"
DEFAULT_STATE_FILE = "/var/lib/daily-trend-radar/last_youtube_ai.txt"
# 送った本文と送信結果(成功/失敗)を日付ごとに恒久保存する先。
# ★weekly_youtube.pyのarchive("/var/lib/daily-trend-radar/archive")とは別ディレクトリにする。
#   同じディレクトリだと、weekly実行日(土曜)にこのdailyジョブも走った場合、同じ日付キーの
#   ファイルを取り合って上書きし合う事故になるため。
DEFAULT_ARCHIVE_DIR = "/var/lib/daily-trend-radar/archive-youtube-ai"
# 一度選んで送った動画のvideo_idを恒久的に記録する台帳(毎日実行での重複表示を防ぐための専用機構)。
DEFAULT_SEEN_FILE = "/var/lib/daily-trend-radar/youtube_ai_seen.json"
# healthchecks pingのURLを置く専用の設定ファイル。config.env(deliver.py/weekly_youtube.pyと共用)
# には絶対に書かない/読まない。weekly_youtube.py用のyoutube-healthcheck.envとも別ファイルにする
# (このジョブ専用にhealthchecks.io側で新規チェックを発行する前提。詳細は実装メモ参照)。
DEFAULT_HEALTHCHECK_CONFIG_PATH = "/etc/daily-trend-radar/youtube-ai-healthcheck.env"

FETCH_TIMEOUT_SEC = 15
LINE_TIMEOUT_SEC = 30

SEARCH_MAX_RESULTS = 10
VIDEOS_CHUNK_SIZE = 50  # videos.listは1回のリクエストでidを最大50件まとめて取れる
MAX_PER_CHANNEL = 1  # TOP_Nが3本と少ないため、weekly版(2本)より絞って多様性を確保する
TOP_N = 3  # 依頼書の上限指定(LINEが長なると読まれんくなるため)
DAYS_BACK = 30  # weekly_youtube.pyと同じ幅。毎日実行での重複はDAYS_BACKでなくSEEN_FILEで防ぐ
MIN_VIEWS = 10000  # これ未満は「伸びとる」と呼べんため除外(weekly_youtube.pyと同一基準)
MAX_ENGLISH = 1  # TOP_Nが3本のため、weekly版(2本)より絞る

# 読み手=Claude Code/ChatGPT/Notion/LINE Bot/VPSを日常的に自分で触る実務家(daily-trend-radar/
# PROMPT.mdの「読む人」節と同一人物)。「動画の編集・SNS集客の手口」はweekly_youtube.py側の
# 担当のため、ここは「AIツール自体の使い方・自動化の手口」に絞る(ニュース語は使わない)。
# 各語を選んだ理由は実装メモ(_youtube-daily-実装メモ-2026-09-14.md)に1行ずつ書いてある。
KEYWORDS = [
    "ChatGPT 使い方 業務効率化",
    "Claude Code 使い方",
    "生成AI 自動化 実践",
    "プロンプト 書き方 コツ",
    "AIエージェント 作り方",
]

# タイトル or 説明文にこの語が1つも含まれん動画は無関係とみなして落とす(大小文字は区別しない)。
# ★weekly_youtube.py(動画編集ジャンル)のRELATED_WORDSとは意図的に別セットにしている。
#   「手口(使い方・やり方)」を表す語を並べ、ニュース系の見出し(発表・リリース等)だけの動画が
#   紛れ込みにくいようにする(ニュース語をここに含めない、が設計上の要)。
RELATED_WORDS = [
    "使い方", "やり方", "作り方", "書き方", "方法", "使ってみた", "してみた",
    "実演", "実践", "活用術", "活用法", "コツ", "テクニック", "ハック", "手順",
    "チュートリアル", "how to", "tutorial", "guide",
]

# タイトルにこの語が含まれとったら無関係として除外する(大小文字は区別しない)。
# 前半はweekly_youtube.pyと共通の除外語(レシピ/ゲーム実況等)。後半はこのジョブ専用で追加した
# 「ニュース語」(依頼書の方針①「ニュースは拾わん」を、手口語のRELATED_WORDSだけに頼らず
# 二重に担保するための安全網。手口語とニュース語が両方入ったタイトルも確実に落とす)。
EXCLUDE_WORDS = [
    "レシピ", "recipe", "料理", "cooking", "cake", "food", "먹방",
    "ゲーム実況", "gameplay", "ASMR", "MV", "Official Video", "歌ってみた",
    "vlog", "ルーティン", "購入品",
    "速報", "発表", "リリース", "ニュース",
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
    # DAILY_YOUTUBE_AI_LOG はローカル検証用の上書き口。本番はデフォルトのまま使う。
    log_path = os.environ.get("DAILY_YOUTUBE_AI_LOG", DEFAULT_LOG_PATH)
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
    # DAILY_TREND_RADAR_CONFIG は他2本と共通のローカル検証用上書き口(同じ設定ファイルのため)。
    config_path = os.environ.get("DAILY_TREND_RADAR_CONFIG", DEFAULT_CONFIG_PATH)
    file_values: dict[str, str] = {}
    if os.path.isfile(config_path):
        file_values = parse_env_file(config_path)
    else:
        logging.warning("設定ファイルが見つからん: %s (環境変数のみで動作)", config_path)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_values.get(key) or default

    config = {
        # ★YOUTUBE_API_KEY / LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID は既存2本と共用の値を
        #   そのまま使い回す。この設計のためにconfig.envへの新規追加値は無い。
        "YOUTUBE_API_KEY": get("YOUTUBE_API_KEY"),
        "LINE_CHANNEL_ACCESS_TOKEN": get("LINE_CHANNEL_ACCESS_TOKEN"),
        "LINE_USER_ID": get("LINE_USER_ID"),
        # 以下3つはこのジョブ専用のオプション値(省略時デフォルトあり)。
        "YOUTUBE_AI_STATE_FILE": get("YOUTUBE_AI_STATE_FILE", DEFAULT_STATE_FILE),
        "YOUTUBE_AI_ARCHIVE_DIR": get("YOUTUBE_AI_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR),
        "YOUTUBE_AI_SEEN_FILE": get("YOUTUBE_AI_SEEN_FILE", DEFAULT_SEEN_FILE),
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


def load_seen_ids(path: str) -> set[str]:
    """送信済み動画のvideo_id台帳を読む(無ければ空集合)。壊れとる場合も空集合扱いにして
    処理は止めない(台帳が壊れて全部の動画が「未送信」に戻る = 最悪でも重複表示が起きる
    だけで、致命的な機能停止にはならない設計)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("video_ids") if isinstance(data, dict) else None
        if isinstance(ids, list):
            return {str(v) for v in ids}
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("seenファイルのパースに失敗した(空の台帳として扱う)")
    return set()


def write_seen_ids(path: str, ids: set[str]) -> None:
    seen_dir = os.path.dirname(path)
    if seen_dir:
        os.makedirs(seen_dir, exist_ok=True)
    tmp_path = f"{path}.tmp"
    payload = {"video_ids": sorted(ids), "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S%z")}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def write_archive(archive_dir: str, date_key: str, record: dict) -> None:
    """送った本文と送信結果(成功/失敗)を日付ごとのJSONで恒久保存する(weekly_youtube.pyの
    2026-08-22改修と同じ設計。保存失敗はログにだけ残し、LINE配信の成否には影響させない)。"""
    try:
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"{date_key}.json")
        payload = dict(record)
        payload["saved_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S%z")
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        logging.info("本文を保存した: %s (status=%s)", path, payload.get("status"))
    except Exception:
        logging.exception("本文の保存に失敗した(送信結果には影響させない)")


def load_healthcheck_url() -> str | None:
    """healthchecks pingのURLを取得する。未設定ならNone(=pingを省略するだけ)。
    config.env(他2本と共用)には触れず、専用の別ファイルから読む。"""
    try:
        hc_config_path = os.environ.get(
            "YOUTUBE_AI_HEALTHCHECK_CONFIG", DEFAULT_HEALTHCHECK_CONFIG_PATH
        )
        file_values: dict[str, str] = {}
        if os.path.isfile(hc_config_path):
            file_values = parse_env_file(hc_config_path)
        return (
            os.environ.get("YOUTUBE_AI_HEALTHCHECK_URL")
            or file_values.get("YOUTUBE_AI_HEALTHCHECK_URL")
            or None
        )
    except Exception:
        logging.exception("healthchecks設定の読み込みに失敗した(pingを省略する)")
        return None


def ping_healthcheck(url: str) -> None:
    """healthchecksへ成功pingを送る。失敗してもここで握りつぶす(本処理の成否に影響させない)。"""
    try:
        req = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "daily-trend-radar-youtube-ai/1.0"}
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            logging.info("healthchecks ping送信: status=%s", resp.status)
    except Exception:
        logging.exception("healthchecks pingに失敗した(本処理の成否には影響させない)")


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
            "User-Agent": "daily-trend-radar-youtube-ai/1.0",
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


def exclude_seen(videos: list[dict], seen_ids: set[str]) -> list[dict]:
    """既に送信済みのvideo_idを候補から除外する(毎日実行での重複表示を防ぐ専用の一手間)。"""
    return [v for v in videos if v.get("video_id") not in seen_ids]


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
    header = f"\U0001F916 今日のAI手口動画（{today_label}）"

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
    logging.info("youtube_ai_howto.py 開始%s", "(dry-run)" if dry_run else "")

    try:
        config = load_config()
        state_path = config["YOUTUBE_AI_STATE_FILE"]
        archive_dir = config["YOUTUBE_AI_ARCHIVE_DIR"]
        seen_path = config["YOUTUBE_AI_SEEN_FILE"]
        today = datetime.now(JST)
        today_key = today.strftime("%Y-%m-%d")
        last_run = read_state(state_path)

        if last_run == today_key:
            logging.info("実行スキップ(本日は実行済み): date=%s", today_key)
            logging.info("youtube_ai_howto.py 正常終了%s", "(dry-run)" if dry_run else "")
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

        seen_ids = load_seen_ids(seen_path)
        logging.info("seen台帳の既存件数: %s", len(seen_ids))

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
            unseen = exclude_seen(filtered, seen_ids)
            logging.info(
                "seen除外完了: 未送信=%s件(フィルタ後%s件中、既送信%s件を除外)",
                len(unseen), len(filtered), len(filtered) - len(unseen),
            )
            ranked = rank_videos(unseen)

        if ranked:
            today_label = f"{today.month}/{today.day}"
            message = build_message(ranked, today_label)
            ja_count = sum(1 for v in ranked if v.get("is_japanese"))
            en_count = len(ranked) - ja_count
            record_base = {
                "date": today_key,
                "video_count": len(ranked),
                "japanese_count": ja_count,
                "english_count": en_count,
                "char_count": len(message),
                "message": message,
                "videos": ranked,
            }
            if dry_run:
                logging.info(
                    "[dry-run] LINE送信をスキップし、本文を標準出力に表示する: 件数=%s(日本語%s/英語%s)",
                    len(ranked), ja_count, en_count,
                )
                write_archive(archive_dir, today_key, dict(record_base, status="dry_run"))
                print(message)
            else:
                http_status: int | None = None
                send_exc: Exception | None = None
                try:
                    http_status = send_line(config["LINE_CHANNEL_ACCESS_TOKEN"], config["LINE_USER_ID"], message)
                except Exception as e:
                    send_exc = e

                if send_exc is not None or http_status != 200:
                    write_archive(
                        archive_dir, today_key,
                        dict(
                            record_base,
                            status="failed",
                            http_status=http_status,
                            error=(str(send_exc) if send_exc is not None else None),
                        ),
                    )
                    if send_exc is not None:
                        raise RuntimeError(f"LINE push失敗(例外)のため中断: {send_exc}") from send_exc
                    raise RuntimeError(f"LINE push失敗のため中断(http={http_status})")

                write_archive(archive_dir, today_key, dict(record_base, status="sent", http_status=http_status))
                # ★送信が成功した分だけseen台帳に追記する(dry-run/失敗時は追記しない。
                #   失敗時に追記すると、実際には届いてへん動画が「送信済み」扱いになって
                #   二度と拾われんくなる事故になるため)。
                new_seen = seen_ids | {v.get("video_id") for v in ranked if v.get("video_id")}
                write_seen_ids(seen_path, new_seen)
                logging.info(
                    "LINE配信完了: 件数=%s(日本語%s/英語%s) http=%s 文字数=%s seen台帳更新後=%s件",
                    len(ranked), ja_count, en_count, http_status, len(message), len(new_seen),
                )
        else:
            logging.info("配信対象なし、LINE送信スキップ")

        if dry_run:
            logging.info("[dry-run] state更新をスキップ")
        else:
            write_state(state_path, today_key)
            # ★成功時だけping(dry-run・送信失敗時はここに到達しない=pingしない)。
            healthcheck_url = load_healthcheck_url()
            if healthcheck_url:
                ping_healthcheck(healthcheck_url)
            else:
                logging.info("healthchecks未設定のためping省略")
        logging.info("youtube_ai_howto.py 正常終了%s", "(dry-run)" if dry_run else "")
        return 0

    except QuotaExceededError as e:
        logging.error("YouTube APIクォータ超過のため中断(リトライはしない): %s", e)
        return 1
    except Exception:
        logging.exception("処理中に異常終了")
        return 1


if __name__ == "__main__":
    sys.exit(main())
