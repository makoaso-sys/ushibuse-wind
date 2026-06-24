#!/usr/bin/env python3
"""
静的ダッシュボード生成 — wind.db から PNG と HTML を出力する。

サーバを立てずに「画像＋HTMLを置くだけ」で、スマホからどこでも見られるようにする。
GitHub Actions(クラウド)で定期実行し、GitHub Pages で公開する想定(README参照)。
PCの電源状態に依存しない。

出力:
  docs/forecast.png   … 複数モデル風予測チャート(m/s, JST, 曜日付き)
  docs/index.html     … それを表示するモバイル向けページ(自動再読込つき)

使い方:
  python generate_dashboard.py            # wind.db -> docs/
  python generate_dashboard.py --db ... --out ...
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
import numpy as np

import phase6_common as pc

MAX_LEAD = 48

# 日本語フォント(無ければ既定にフォールバック)
for _f in ("Noto Sans CJK JP", "Noto Sans CJK", "IPAGothic", "sans-serif"):
    plt.rcParams["font.family"] = _f
    break
plt.rcParams["axes.unicode_minus"] = False

_WD = ["月", "火", "水", "木", "金", "土", "日"]   # weekday() 0..6


def _fmt_date(x, pos=None):
    d = mdates.num2date(x)
    return f"{d.month}/{d.day}({_WD[d.weekday()]})\n{d:%H:%M}"


def _jst_naive(iso: str):
    return datetime.fromisoformat(iso).astimezone(pc.JST).replace(tzinfo=None)


# ============================================================
# チャート生成
# ============================================================
def make_chart(conn, fa: str, path: str) -> None:
    models = [r["model"] for r in conn.execute(
        "SELECT DISTINCT model FROM forecasts WHERE fetched_at=? ORDER BY model", (fa,))]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6.4), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    colors = {"jma_msm": "#d6336c", "jma_gsm": "#f08c00",
              "ecmwf_ifs025": "#1f5fa5", "gfs_seamless": "#2f9e44"}
    t0 = None
    for m in models:
        rows = conn.execute(
            """SELECT valid_time, wind_speed_ms FROM forecasts
               WHERE fetched_at=? AND model=? AND lead_hours<=?
               ORDER BY valid_time""", (fa, m, MAX_LEAD)).fetchall()
        if not rows:
            continue
        t = [_jst_naive(r["valid_time"]) for r in rows]
        ms = [r["wind_speed_ms"] for r in rows]
        t0 = t0 or t[0]
        lw = 2.8 if m == "jma_msm" else 1.5
        ax1.plot(t, ms, label=m + ("（本命）" if m == "jma_msm" else ""),
                 color=colors.get(m, "#888"), linewidth=lw, marker="o", markersize=2.5)

    ax1.axhspan(pc.SAIL_MIN_MS, pc.SAIL_MAX_MS, color="#2f9e44", alpha=0.08)
    ax1.axhline(pc.SAIL_MIN_MS, color="#2f9e44", ls="--", lw=1, alpha=.6)
    ax1.axhline(pc.SAIL_MAX_MS, color="#e8590c", ls="--", lw=1, alpha=.6)
    if t0:
        ax1.text(t0, pc.SAIL_MAX_MS + 0.3, "出走レンジ", color="#2f9e44", fontsize=9)
    ax1.set_ylabel("風速 (m/s)")
    ax1.set_ylim(0, max(pc.SAIL_MAX_MS + 2, 16))
    _fa = datetime.fromisoformat(fa).astimezone(pc.JST)
    ax1.set_title(f"Ushibuse Beach Multi-model Wind Forecast  (issued {_fa.month}/{_fa.day}"
                  f"({_WD[_fa.weekday()]}) {_fa:%H:%M} JST)", fontsize=12)
    ax1.legend(fontsize=8, ncol=4, loc="upper right")
    ax1.grid(alpha=.25)

    # 出走判定の対象時刻(次の2時刻)に縦線
    for vtiso in pc.next_n_clock_valid_times(conn, fa, pc.TARGET_HOURS_JST):
        x = _jst_naive(vtiso)
        hour = datetime.fromisoformat(vtiso).astimezone(pc.JST).hour
        ax1.axvline(x, color="#6741d9", ls=":", lw=1.3, alpha=.7)
        ax1.text(x, ax1.get_ylim()[1] * 0.97, f"{hour:02d}:00", color="#6741d9",
                 fontsize=8, ha="center", va="top")

    # 風向(jma_msm) 一定長・中央ピボットの矢印
    rows = conn.execute(
        """SELECT valid_time, wind_u, wind_v FROM forecasts
           WHERE fetched_at=? AND model='jma_msm' AND lead_hours<=?
           ORDER BY valid_time""", (fa, MAX_LEAD)).fetchall()
    if rows:
        step = 3
        t = [_jst_naive(r["valid_time"]) for r in rows][::step]
        u = np.array([r["wind_u"] for r in rows][::step], dtype=float)
        v = np.array([r["wind_v"] for r in rows][::step], dtype=float)
        mag = np.hypot(u, v); mag[mag == 0] = 1.0
        ax2.axhline(0, color="#ccc", lw=0.8)
        ax2.quiver(t, np.zeros(len(t)), u / mag, v / mag, color="#1f5fa5",
                   angles="uv", scale_units="inches", scale=2.2, width=0.004,
                   headwidth=4, headlength=5, pivot="mid")
    ax2.set_ylim(-1, 1); ax2.set_yticks([])
    ax2.set_ylabel("風向\n(jma_msm)", fontsize=9)
    ax2.text(0.005, 0.97, "矢印 = 風の進む向き（上が北）", transform=ax2.transAxes,
             fontsize=8, va="top", color="#555")
    ax2.xaxis.set_major_formatter(FuncFormatter(_fmt_date))
    ax2.grid(alpha=.2, axis="x")
    plt.tight_layout()
    plt.savefig(path, dpi=110)
    plt.close(fig)


# ============================================================
# HTML 生成
# ============================================================
HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>Ushibuse Beach Wind Forecast</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px;
         max-width: 900px; margin-inline: auto; }}
  h1 {{ font-size: 1.3rem; margin: .2em 0; }}
  .updated {{ color: #888; font-size: .85rem; margin: 0 0 12px; }}
  .cards {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }}
  .card {{ flex: 1 1 140px; border: 1px solid #ccc8; border-radius: 12px;
          padding: 12px 14px; }}
  .card .lead {{ font-size: .8rem; color: #888; }}
  .card .v {{ font-size: 1.5rem; font-weight: 700; }}
  .card .verdict {{ font-size: 1rem; margin-top: 4px; }}
  .ok {{ color: #2f9e44; }}
  .no {{ color: #e8590c; }}
  img {{ width: 100%; height: auto; border: 1px solid #ccc8; border-radius: 12px; }}
  .note {{ color: #888; font-size: .8rem; margin-top: 10px; }}
</style>
</head>
<body>
<h1>🏄 Ushibuse Beach Wind Forecast</h1>
<p class="updated">最終更新: {updated} JST　/　出走レンジ {smin:.0f}–{smax:.0f} m/s</p>
<div class="cards">{cards}</div>
<img src="forecast.png?v={ver}" alt="複数モデル風予測">
<p class="note">出走判定は 10:00 / 14:00（JST）時点の予測に基づく。複数モデルの生予測の合議（補正前）。
jma_msm(5km) が局地の本命。このページは約30分ごとに自動再読込します。</p>
<p class="note">【出走判定条件】風速 {smin:.0f}〜{smax:.0f} m/s ／ 風向 NE〜S〜W（45°〜270°）／ {nmin} モデル以上が条件を満たすこと</p>
</body>
</html>
"""

CARD = """<div class="card">
  <div class="lead">{when}</div>
  <div class="v">{speed:.1f} m/s</div>
  <div>{compass}（{deg}°）・合議 {agree}</div>
  <div class="verdict {cls}">{verdict}</div>
</div>"""


def make_html(conn, fa: str, path: str) -> None:
    cards = []
    for vt_iso in pc.next_n_clock_valid_times(conn, fa, pc.TARGET_HOURS_JST):
        ev = pc.evaluate_at(conn, vt_iso, fa)
        if not ev:
            continue
        vt = ev["valid_time_jst"]
        when = vt.strftime(f"%-m/%-d({_WD[vt.weekday()]}) %H:%M") if vt else "-"
        cards.append(CARD.format(
            when=when, speed=ev["mean_speed_ms"],
            compass=ev["mean_compass"], deg=ev["mean_dir_deg"], agree=ev["agree"],
            cls="ok" if ev["sailable"] else "no",
            verdict="✅ 出走可" if ev["sailable"] else "⚠️ 見送り"))
    _fa = datetime.fromisoformat(fa).astimezone(pc.JST)
    updated = f"{_fa.month}/{_fa.day}({_WD[_fa.weekday()]}) {_fa:%H:%M}"
    ver = re.sub(r"\D", "", fa)[:12]
    html = HTML.format(updated=updated, smin=pc.SAIL_MIN_MS, smax=pc.SAIL_MAX_MS,
                       nmin=pc.MIN_MODELS_AGREE, cards="".join(cards), ver=ver)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="wind.db")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()

    conn = pc.connect(args.db)
    fa = pc.latest_fetched_at(conn)
    if not fa:
        print("予測データがありません。先に collect_forecasts.py を実行してください。")
        return
    os.makedirs(args.out, exist_ok=True)
    make_chart(conn, fa, os.path.join(args.out, "forecast.png"))
    make_html(conn, fa, os.path.join(args.out, "index.html"))
    print(f"生成完了: {args.out}/index.html, {args.out}/forecast.png  (発表 {fa} UTC)")


if __name__ == "__main__":
    main()
