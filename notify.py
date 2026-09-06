#!/usr/bin/env python3
"""
フェーズ6 通知 — 出走できそうな時に スマホへプッシュ通知する。

通知先は ntfy(無料・アカウント不要・OSS)を既定にしている。
  1. スマホに ntfy アプリを入れる(iOS/Android)
  2. 適当に推測されにくいトピック名を決めて購読する(例: breeze-play-ushibuse-9f3k2)
  3. 下の NTFY_TOPIC を同じ名前にする
それだけで、このスクリプトが POST した内容がスマホに届く。

LINE で特定の人に届けたい場合は、LINE公式アカウント(Messaging API)の push を
併用する。LINE Notify は 2025-03-31 に終了したため、個人トークン方式は使えない。
設定は README_phase6.md「3. LINE で特定の人に通知する」を参照。
  LINE_CHANNEL_ACCESS_TOKEN … チャネルアクセストークン(長期)
  LINE_TO                   … 宛先ID(userId / groupId / roomId)。カンマ区切りで複数可

使い方:
  python notify.py --dry-run        # 送信せず内容だけ表示(動作確認)
  python notify.py                  # 実際に送信(設定済みのチャネル全部へ)
  python notify.py --topic breeze-play-ushibuse-9f3k2 --db /data/wind.db
  python notify.py --no-ntfy        # LINE だけに送る

cron 例(取得の後、1日2回判定):
  20 0,12 * * * cd /path && python3 notify.py >> notify.log 2>&1
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone

import requests

import phase6_common as pc

# ============================================================
# 設定
# ============================================================
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = "breeze-play-ushibuse-9f3k2"
DB_PATH = "wind.db"
NOTIFY_ONLY_WHEN_SAILABLE = True         # 走れそうな時だけ通知(False=毎回状況を送る)

# LINE Messaging API。宛先1件につき1リクエスト送る。multicast は1リクエストで
# 複数人に配れるが userId 専用でグループに送れないため、宛先の種類を選ばない
# push に統一する(数人規模ならリクエスト数の差は問題にならない)。
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 5000                   # テキストメッセージ1通の上限(文字)
LINE_ATTEMPTS = 2                        # 一時障害(429/5xx)のみ、この回数まで試す
LINE_RETRY_WAIT_SEC = 3


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
    src = "補正済み加重平均" if ev.get("speed_source") == "corrected_weighted" \
          else "単純平均(補正データなし)"
    body = (f"{when} 予測\n"
            f"風速 {ev['mean_speed_ms']}m/s / 風向 {ev['mean_compass']}"
            f"({ev['mean_dir_deg']}°)\n"
            f"出走レンジ {pc.SAIL_MIN_MS:.0f}〜{pc.SAIL_MAX_MS:.0f}m/s で判定\n"
            f"({src}／モデル別内訳 {ev['agree']})")
    tags = "surfer" if ev["sailable"] else "thinking"
    return head, body, tags


_PRIORITY_MAP = {"max": 5, "urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}


def send_ntfy(server: str, topic: str, title: str, body: str, tags: str,
              priority: str = "default") -> bool:
    try:
        r = requests.post(server, json={
            "topic": topic,
            "title": title,
            "message": body,
            "tags": [tags],
            "priority": _PRIORITY_MAP.get(priority, 3),
        }, timeout=20)
        if r.status_code == 200:
            return True
        print(f"  ntfy エラー HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"  ntfy 送信失敗: {e}", file=sys.stderr)
    return False


# ============================================================
# LINE(Messaging API)送信
# ============================================================
def dest_digest(dest: str) -> str:
    """宛先IDを短いハッシュにする。

    重複抑止キーは wind.db に入り、公開リポジトリへ push される。LINE の
    userId はその人を指す識別子なので、生のまま置かない。
    """
    return hashlib.sha256(dest.encode()).hexdigest()[:12]


def send_line(token: str, dest: str, text: str) -> bool:
    """LINE公式アカウントから1宛先へ push する。成否だけを返す。"""
    payload = {"to": dest,
               "messages": [{"type": "text", "text": text[:LINE_TEXT_LIMIT]}]}
    headers = {"Authorization": f"Bearer {token}"}
    short = dest_digest(dest)
    for attempt in range(1, LINE_ATTEMPTS + 1):
        try:
            r = requests.post(LINE_PUSH_URL, json=payload, headers=headers,
                              timeout=20)
        except requests.RequestException as e:
            print(f"  LINE 送信失敗 (to {short}): {e}", file=sys.stderr)
            retryable = True
        else:
            if r.status_code == 200:
                return True
            # 応答本文だけ出す。トークンはヘッダにしかないのでログに漏れない。
            print(f"  LINE エラー HTTP {r.status_code} (to {short}): "
                  f"{r.text[:160]}", file=sys.stderr)
            # 401/403(トークン誤り・友だち未追加)や 400(宛先IDが不正)は
            # 粘っても直らない。429(送信上限)と 5xx だけやり直す。
            retryable = r.status_code == 429 or r.status_code >= 500
        if not retryable or attempt == LINE_ATTEMPTS:
            return False
        time.sleep(LINE_RETRY_WAIT_SEC)
    return False


def parse_line_dests(raw: str) -> list[str]:
    """カンマ/空白区切りの宛先IDを配列にする(CI の未設定は空文字で来る)。"""
    return [d for d in (x.strip() for x in raw.replace("\n", ",").split(",")) if d]


# ============================================================
# メイン
# ============================================================
def run(db_path: str, topic: str, server: str, dry: bool,
        use_ntfy: bool = True, line_token: str = "",
        line_dests: list[str] | None = None) -> None:
    line_dests = line_dests or []
    conn = pc.connect(db_path)
    ensure_log_table(conn)
    fa = pc.latest_fetched_at(conn)
    if not fa:
        print("予測データがありません。先に collect_forecasts.py を実行してください。")
        return
    channels = (["ntfy"] if use_ntfy else []) \
        + ([f"LINE×{len(line_dests)}"] if line_dests else [])
    print(f"判定スナップショット: {fa} (UTC) / 通知先: "
          f"{'・'.join(channels) if channels else 'なし'}")

    for vt_iso in pc.next_n_clock_valid_times(conn, fa, pc.TARGET_HOURS_JST):
        ev = pc.evaluate_at(conn, vt_iso, fa)
        if not ev:
            continue
        verdict = "出走可" if ev["sailable"] else "見送り"
        vt = ev["valid_time_jst"]
        when = vt.strftime("%m/%d %H:%M") if vt else ev["label"]
        print(f"  {when} JST: {verdict}  加重平均{ev['mean_speed_ms']}m/s "
              f"{ev['mean_compass']}  モデル別{ev['agree']}")

        if NOTIFY_ONLY_WHEN_SAILABLE and not ev["sailable"]:
            continue

        # 重複抑止キー = 予測対象日時(その時刻ちょうど)。
        # 送信先ごとに別のキーにする。同じ予報でも「ntfy は成功、LINE は失敗」が
        # 起こりうるので、失敗した宛先だけを次回に持ち越したい。
        base_key = f"{vt.strftime('%Y-%m-%dT%H') if vt else '?'}:{ev['sailable']}"
        title, body, tags = build_message(ev)
        prio = "high" if ev["sailable"] else "default"

        if dry:
            print(f"     [DRY-RUN] {title}\n     " + body.replace("\n", "\n     "))
            for name, key in _targets(base_key, use_ntfy, line_dests):
                state = "送信済みのためスキップ" if already_sent(conn, key) else "送る"
                print(f"     [DRY-RUN] {name}: {state}")
            continue

        # ntfy は従来のキーのまま(接尾辞を足すと既存 notify_log と一致せず、
        # 通知済みの予報をもう一度送ってしまう)
        if use_ntfy:
            if already_sent(conn, base_key):
                print(f"     (ntfy 通知済み: {base_key})")
            elif send_ntfy(server, topic, title, body, tags, prio):
                mark_sent(conn, base_key)
                print(f"     ntfy 送信: {title}")

        text = f"{title}\n{body}"
        for dest in line_dests:
            key = f"{base_key}:line:{dest_digest(dest)}"
            if already_sent(conn, key):
                print(f"     (LINE 通知済み: {dest_digest(dest)})")
                continue
            if not line_token:
                print("     LINE: 宛先はあるがトークン未設定のため送らない",
                      file=sys.stderr)
                break
            if send_line(line_token, dest, text):
                mark_sent(conn, key)
                print(f"     LINE 送信: {dest_digest(dest)} 宛")

    conn.close()


def _targets(base_key: str, use_ntfy: bool,
             line_dests: list[str]) -> list[tuple[str, str]]:
    """(表示名, 重複抑止キー) の一覧。--dry-run の内訳表示に使う。"""
    out = [("ntfy", base_key)] if use_ntfy else []
    out += [(f"LINE {dest_digest(d)}", f"{base_key}:line:{dest_digest(d)}")
            for d in line_dests]
    return out


def main():
    ap = argparse.ArgumentParser(description="牛臥海岸 出走通知")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--topic", default=NTFY_TOPIC)
    ap.add_argument("--server", default=NTFY_SERVER)
    ap.add_argument("--dry-run", action="store_true", help="送信せず内容だけ表示")
    ap.add_argument("--no-ntfy", action="store_true",
                    help="ntfy へは送らない(LINE だけ使う)")
    ap.add_argument("--line-token", default="",
                    help="LINE チャネルアクセストークン"
                         "(既定: 環境変数 LINE_CHANNEL_ACCESS_TOKEN)")
    ap.add_argument("--line-to", default="",
                    help="LINE 宛先ID。カンマ区切りで複数可"
                         "(既定: 環境変数 LINE_TO)")
    args = ap.parse_args()
    # CI から未設定の Secret を渡すと空文字で来る。空トピックへ POST すると
    # ntfy が 400 を返して黙って失敗するので、既定トピックに落とす。
    topic = args.topic.strip() or NTFY_TOPIC
    if not args.dry_run and not args.no_ntfy and "CHANGE-ME" in topic:
        print("先に NTFY_TOPIC を変更するか --topic を指定してください。", file=sys.stderr)
        sys.exit(1)

    # LINE は「設定されていれば使う」。未設定なら黙って ntfy だけで動く
    # (Secrets を入れるまで、このスクリプトの挙動は今までと変わらない)。
    line_token = args.line_token.strip() or \
        os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_dests = parse_line_dests(args.line_to or os.environ.get("LINE_TO", ""))
    if line_token and not line_dests:
        print("警告: LINE トークンはあるが LINE_TO(宛先ID)が空。LINE へは送らない。",
              file=sys.stderr)
    if args.no_ntfy and not line_dests:
        print("--no-ntfy だが LINE の宛先もない。通知先がゼロ。", file=sys.stderr)
        sys.exit(1)

    run(args.db, topic, args.server, args.dry_run,
        use_ntfy=not args.no_ntfy, line_token=line_token, line_dests=line_dests)


if __name__ == "__main__":
    main()
