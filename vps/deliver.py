#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily-trend-radar: 毎朝7:30(JST)にレポート(latest.json)を取得してLINEに配信する。

流儀はVPSの既存実装に合わせている:
- LINE Messaging APIのpushはinventory-bot/scheduler.pyと同じエンドポイント・payload形。
- healthchecks.ioへのpingはvps-intrusion-check.shと同じ「打てたかログに残すだけ、
  失敗しても本体は落とさない」流儀。
- 設定ファイルは/etc/xxx/config.envにKEY=VALUEで置く(vps-intrusion-check.confと同型)。

方針:
- レポートのdateがSTATE_FILEに記録済みの日付と同じなら何もしない(二重配信防止)。
- count==0の日はLINEを送らない(ネタが無い日は黙る)。ただしSTATE_FILEは更新する。
- 送信・スキップいずれでも処理が最後まで正常に走ったらhealthchecksにpingを打つ。
- ネットワークエラー・JSONパース失敗・LINE APIのエラーレスポンスは、ログに残した上で
  非ゼロ終了する(=pingを打たない=監視が鳴る)。例外を握りつぶして正常終了しない。
- レポートのdateの古さ(age)をJST基準でチェックする(生成側=クラウドのroutineが止まって
  latest.jsonが何日も更新されなくなっても、このスクリプトが「前回と同じ日付やからスキップ」
  で正常終了し続けてpingを打ち、監視だけ緑のままになる事故を防ぐため):
    - 0日(今日)          … 通常通り処理を続ける
    - 1日前              … 警告ログを出して続行する(pingは打つ)
    - 2日以上前/パース失敗 … 例外を投げて非ゼロ終了する(pingは打たない)
- `--dry-run` を付けて実行すると、LINE送信・STATE_FILE更新・healthchecks pingを一切行わず、
  組み上げるはずだった本文を標準出力に表示するだけで終わる(送信経路のテストが本番の
  LINE送信や状態ファイルを汚さないようにするための穴)。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MESSAGE_LIMIT = 5000

DEFAULT_CONFIG_PATH = "/etc/daily-trend-radar/config.env"
DEFAULT_LOG_PATH = "/var/log/daily-trend-radar.log"
DEFAULT_REPORT_JSON_URL = (
    "https://raw.githubusercontent.com/nynyzeni-dot/daily-trend-radar/main/latest.json"
)
DEFAULT_STATE_FILE = "/var/lib/daily-trend-radar/last_delivered.txt"

FETCH_TIMEOUT_SEC = 15
LINE_TIMEOUT_SEC = 30
PING_TIMEOUT_SEC = 10


class JSTFormatter(logging.Formatter):
    """ログのタイムスタンプを常にJSTで出す(サーバーのTZ設定に依存しない)。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=JST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")


def setup_logging() -> None:
    # DAILY_TREND_RADAR_LOG はローカル検証用の上書き口。本番はデフォルトのまま使う。
    log_path = os.environ.get("DAILY_TREND_RADAR_LOG", DEFAULT_LOG_PATH)
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
    # DAILY_TREND_RADAR_CONFIG はローカル検証用の上書き口。本番はデフォルトのまま使う。
    config_path = os.environ.get("DAILY_TREND_RADAR_CONFIG", DEFAULT_CONFIG_PATH)
    file_values: dict[str, str] = {}
    if os.path.isfile(config_path):
        file_values = parse_env_file(config_path)
    else:
        logging.warning("設定ファイルが見つからん: %s (環境変数のみで動作)", config_path)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_values.get(key) or default

    config = {
        "LINE_CHANNEL_ACCESS_TOKEN": get("LINE_CHANNEL_ACCESS_TOKEN"),
        "LINE_USER_ID": get("LINE_USER_ID"),
        "LINE_PING_URL": get("LINE_PING_URL"),
        "REPORT_JSON_URL": get("REPORT_JSON_URL", DEFAULT_REPORT_JSON_URL),
        "STATE_FILE": get("STATE_FILE", DEFAULT_STATE_FILE),
    }

    missing = [k for k in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID") if not config[k]]
    if missing:
        raise RuntimeError(f"必須設定が不足しとる: {', '.join(missing)}")

    return config  # type: ignore[return-value]


def fetch_report(url: str) -> dict:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "daily-trend-radar-deliver/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"レポート取得に失敗(HTTP {e.code}): {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"レポート取得に失敗(ネットワークエラー): {url}: {e}") from e

    if status != 200:
        raise RuntimeError(f"レポート取得に失敗(HTTP {status}): {url}")

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"レポートJSONのパースに失敗: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError("レポートJSONの形式が想定外(dictでない)")

    for required_key in ("date", "count"):
        if required_key not in data:
            raise RuntimeError(f"レポートJSONに必須キーが無い: {required_key}")

    data.setdefault("items", [])
    data.setdefault("url", "")
    return data


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


def check_report_freshness(report_date_str: str, now: datetime) -> None:
    """report["date"]の古さ(age)をJST基準でチェックする。

    生成側(クラウドのroutine)が止まってlatest.jsonが何日も更新されなくなっても、
    このスクリプトが「前回と同じ日付やからスキップ」で正常終了し続けてpingを打ち、
    LINEは1通も来んのに監視だけ緑のまま、という一番タチの悪い壊れ方を防ぐガード。

    - age<=0(今日)         … 何もしない
    - age==1(1日前)        … 警告ログを出して続行する(呼び出し元はpingを打つ)
    - age>=2/パース失敗    … RuntimeErrorを投げる(呼び出し元は非ゼロ終了・pingを打たない)
    """
    try:
        report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError) as e:
        raise RuntimeError(
            f"レポートのdateがパースできん(形式異常のため生成側の異常を疑う): {report_date_str!r}"
        ) from e

    age_days = (now.date() - report_date).days

    if age_days <= 0:
        return
    if age_days == 1:
        logging.warning(
            "レポートが1日前のまま更新されていない(生成側が止まっている可能性): date=%s",
            report_date_str,
        )
        return
    raise RuntimeError(
        f"レポートが{age_days}日前のまま更新されていない(生成側が止まっている可能性): date={report_date_str}"
    )


def build_message(report: dict) -> str:
    date_str = report["date"]
    try:
        year, month, day = date_str.split("-")
        header_date = f"{int(month)}/{int(day)}"
    except (ValueError, AttributeError):
        header_date = date_str

    header = f"\U0001F4F9 今日のトレンド（{header_date}）"
    footer = f"ぜんぶ見る → {report.get('url', '')}"
    items = report.get("items") or []

    # 5000文字に収まるまで末尾から間引く。footerは必ず残す。
    keep = len(items)
    while True:
        lines = [header, ""]
        for item in items[:keep]:
            title = (item.get("title") or "").strip()
            why = (item.get("why") or "").strip()
            item_url = (item.get("url") or "").strip()
            lines.append(f"▪ {title}")
            if why:
                lines.append(f"  {why}")
            if item_url:
                lines.append(f"  {item_url}")
            lines.append("")
        lines.append(footer)
        message = "\n".join(lines)
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


def ping(url: str | None) -> None:
    if not url:
        logging.warning("LINE_PING_URL 未設定のためping送信をスキップ")
        return
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=PING_TIMEOUT_SEC) as resp:
            logging.info("healthchecks ping OK (http=%s)", resp.status)
    except Exception as e:
        # ping自体の失敗で本体を落とさない(vps-intrusion-check.shと同じ流儀)
        logging.warning("healthchecks ping失敗(無視して続行): %s", e)


def main() -> int:
    setup_logging()
    dry_run = "--dry-run" in sys.argv[1:]
    logging.info("daily-trend-radar deliver.py 開始%s", "(dry-run)" if dry_run else "")

    try:
        config = load_config()
        report = fetch_report(config["REPORT_JSON_URL"])
        report_date = report["date"]
        count = report["count"]

        # レポートが古いまま(生成側停止の疑い)なら、ここで例外を投げて非ゼロ終了させる。
        # last_delivered比較より先に行う: state側だけ古い日付のまま止まっていても
        # 「スキップ扱いで正常終了→ping」になってしまうのを防ぐため。
        check_report_freshness(report_date, datetime.now(JST))

        state_path = config["STATE_FILE"]
        last_delivered = read_state(state_path)

        logging.info(
            "レポート取得完了: date=%s count=%s last_delivered=%s",
            report_date, count, last_delivered,
        )

        if last_delivered == report_date:
            logging.info("配信スキップ(既に配信済みの日付): date=%s", report_date)
        elif not count:
            logging.info("配信スキップ(ネタなし count=0): date=%s", report_date)
            if dry_run:
                logging.info("[dry-run] state更新をスキップ")
            else:
                write_state(state_path, report_date)
        else:
            message = build_message(report)
            if dry_run:
                logging.info("[dry-run] LINE送信をスキップし、本文を標準出力に表示する")
                print(message)
            else:
                status = send_line(config["LINE_CHANNEL_ACCESS_TOKEN"], config["LINE_USER_ID"], message)
                if status != 200:
                    # ここでraiseしてping前にプロセスを非ゼロ終了させる(監視が鳴る)
                    raise RuntimeError(f"LINE push失敗のため中断(http={status})")
                logging.info(
                    "LINE配信完了: date=%s count=%s http=%s 文字数=%s",
                    report_date, count, status, len(message),
                )
                write_state(state_path, report_date)

        if dry_run:
            logging.info("[dry-run] healthchecks pingをスキップ")
        else:
            ping(config["LINE_PING_URL"])
        logging.info("daily-trend-radar deliver.py 正常終了%s", "(dry-run)" if dry_run else "")
        return 0

    except Exception:
        logging.exception("処理中に異常終了(pingは打たない)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
