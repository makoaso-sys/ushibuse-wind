#!/usr/bin/env python3
"""
フェーズ6 通知 — 出走できそうな時に スマホへプッシュ通知する。

通知先は ntfy(無料・アカウント不要・OSS)を既定にしている。
  1. スマホに ntfy アプリを入れる(iOS/Android)
  2. 適当に推測されにくいトピック名を決めて購読する(例: breeze-play-ushibuse-9f3k2)
  3. 下の NTFY_TOPIC を同じ名前にする
それだけで、このスクリプトが POST した内容がスマホに届く。

使い方:
  python notify.py --dry-run        # 送信せず内容だけ表示(動作確認)
  python notify.py                  # 実際に送信
  python notify.py --topic breeze-play-ushibuse-9f3k2 --db /data/wind.db

cron 例(取得の後、1日2回判定):
  20 0,12 * * * cd /path && python3 notify.py >> notify.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import urllib.parse

import requests

import phase6_common as pc

# ============================================================
# 設定
# ============================================================
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = "breeze-play-ushibuse-9f3k2"
DB_PATH = "wind.db"
NOTIFY_ONLY_WHEN_SAILABLE = True         # 走れそうな時だけ通知(False=毎回状況を送る)


# ============================================================
# 重複通知の抑止(同じ予測対象日×リードは1回だけ送る)
# ============================================================
def ensure_log_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS notify_log(
        key TEXT PRIMARY KEY, sent_at TEXT)""")
    conn.commit()


def already_sent(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM notify_log WHERE key=?", (key,)).fetchone() \
        is not None


def mark_sent(conn, key: str):
    conn.execute("INSERT OR REPLACE INTO notify_log(key, sent_at) VALUES(?,?)",
                 (key, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ============================================================
# メッセージ生成
# ============================================================
def build_message(ev: dict) -> tuple[str, str, str]:
    vt = ev["valid_time_jst"]
    when = vt.strftime("%m/%d %H:%M JST") if vt else "?"
    slot = ev.get("label", "")
    head = f"🏄 出走チャンス {slot}" if ev["sailable"] else f"… 微妙 {slot}"
    body = (f"{when} 予測\n"
            f"風速 {ev['mean_speed_ms']}m/s / 風向 {ev['mean_compass']}"
            f"({ev['mean_dir_deg']}°)\n"
            f"モデル合議 {ev['agree']} が出走可\n"
            f"(生予測の合議・補正前)")
    tags = "surfer" if ev["sailable"] else "thinking"
    return head, body, tags


def send_ntfy(server: str, topic: str, title: str, body: str, tags: str,
              priority: str = "default") -> bool:
    url = f"{server}/{topic}"
    try:
        r = requests.post(url, data=body.encode("utf-8"),
                          headers={"Title": urllib.parse.quote(title),
                                   "Tags": tags,
                                   "Priority": priority}, timeout=20)
        if r.status_code == 200:
            return True
        print(f"  ntfy エラー HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"  ntfy 送信失敗: {e}", file=sys.stderr)
    return False


# ============================================================
# メイン
# ============================================================
def run(db_path: str, topic: str, server: str, dry: bool) -> None:
    conn = pc.connect(db_path)
    ensure_log_table(conn)
    fa = pc.latest_fetched_at(conn)
    if not fa:
        print("予測データがありません。先に collect_forecasts.py を実行してください。")
        return
    print(f"判定スナップショット: {fa} (UTC)")

    for vt_iso in pc.next_n_clock_valid_times(conn, fa, pc.TARGET_HOURS_JST):
        ev = pc.evaluate_at(conn, vt_iso, fa)
        if not ev:
            continue
        verdict = "出走可" if ev["sailable"] else "見送り"
        vt = ev["valid_time_jst"]
        when = vt.strftime("%m/%d %H:%M") if vt else ev["label"]
        print(f"  {when} JST: {verdict}  平均{ev['mean_speed_ms']}m/s "
              f"{ev['mean_compass']}  合議{ev['agree']}")

        if NOTIFY_ONLY_WHEN_SAILABLE and not ev["sailable"]:
            continue

        # 重複抑止キー = 予測対象日時(その時刻ちょうど)
        key = f"{vt.strftime('%Y-%m-%dT%H') if vt else '?'}:{ev['sailable']}"
        if already_sent(conn, key):
            print(f"     (既に通知済み: {key})")
            continue

        title, body, tags = build_message(ev)
        prio = "high" if ev["sailable"] else "default"
        if dry:
            print(f"     [DRY-RUN] {title}\n     " + body.replace("\n", "\n     "))
            continue
        if send_ntfy(server, topic, title, body, tags, prio):
            mark_sent(conn, key)
            print(f"     通知送信: {title}")

    conn.close()


def main():
    ap = argparse.ArgumentParser(description="牛臥海岸 出走通知")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--topic", default=NTFY_TOPIC)
    ap.add_argument("--server", default=NTFY_SERVER)
    ap.add_argument("--dry-run", action="store_true", help="送信せず内容だけ表示")
    args = ap.parse_args()
    if not args.dry_run and "CHANGE-ME" in args.topic:
        print("先に NTFY_TOPIC を変更するか --topic を指定してください。", file=sys.stderr)
        sys.exit(1)
    run(args.db, args.topic, args.server, args.dry_run)


if __name__ == "__main__":
    main()
