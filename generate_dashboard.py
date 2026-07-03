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
import json
import math
import os
import re
import sqlite3
from datetime import datetime


import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
from matplotlib.font_manager import FontProperties
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Circle as _MplCircle
import numpy as np

import phase6_common as pc

MAX_LEAD = 48

_LAT = 35.074094   # 牛臥海岸
_LON = 138.868262

# 日本語フォント: インストール済みのものを優先順に選択
_available_fonts = {f.name for f in fm.fontManager.ttflist}
for _f in ("Noto Sans CJK JP", "Noto Sans CJK", "Hiragino Sans", "YuGothic",
           "BIZ UDGothic", "IPAGothic", "AppleGothic", "sans-serif"):
    if _f in _available_fonts or _f == "sans-serif":
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

# 天気シンボル用フォント(☀☁☔ が入っているもの。DejaVu Sans を優先)
_sym_fp = None
for _fn in ("DejaVu Sans", "Apple Symbols"):
    if _fn in _available_fonts:
        _sym_fp = FontProperties(family=_fn)
        break

_WD = ["月", "火", "水", "木", "金", "土", "日"]   # weekday() 0..6

# 凡例の表示順
_LEGEND_ORDER = ["jma_msm", "jma_gsm", "gfs_seamless", "ecmwf_ifs025"]


def _is_daytime(dt_jst) -> bool:
    """日の出〜日の入りの間なら True(天文計算、外部ライブラリ不要)。"""
    n = dt_jst.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (n - 81))))
    lat_r = math.radians(_LAT)
    cos_ha = -math.tan(lat_r) * math.tan(decl)
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))          # 日照半角(度)
    b = math.radians(360 / 365 * (n - 81))
    eot = (9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)) / 60
    solar_noon_utc = 12 - _LON / 15 - eot
    sunrise_utc = solar_noon_utc - ha / 15
    sunset_utc  = solar_noon_utc + ha / 15
    utc_h = (dt_jst.hour + dt_jst.minute / 60) - 9   # JST -> UTC
    if utc_h < 0:
        utc_h += 24
    return sunrise_utc <= utc_h <= sunset_utc


def _temp_color(temp) -> str:
    """気温 -> 段階的な色  赤/オレンジ/黄/水色/青"""
    if temp is None:   return "#aaa"
    if temp >= 30:     return "#E53935"   # 赤
    if temp >= 20:     return "#FB8C00"   # オレンジ
    if temp >= 10:     return "#FDD835"   # 黄
    if temp >= 0:      return "#29B6F6"   # 水色
    return             "#1565C0"          # 青


def _draw_crescent(ax, x_date, y_data, color="#FFB300"):
    """塗りつぶし三日月を2つの円パッチで描画する。"""
    da = DrawingArea(24, 24, 12, 12)
    da.add_artist(_MplCircle((0, 0),      9,   color=color, zorder=2))
    da.add_artist(_MplCircle((-3.5, 1.5), 7.5, color="white", zorder=3))
    ab = AnnotationBbox(da, (mdates.date2num(x_date), y_data),
                        xycoords="data", frameon=False, box_alignment=(0.5, 0.5),
                        zorder=5)
    ax.add_artist(ab)


def _wmo_label(code, daytime: bool = True) -> tuple[str, str]:
    """WMO天気コード -> (シンボル, 文字色)  ☀/☽=晴  ☁=曇/霧  ☔=雨/雪/雷"""
    if code is None:
        return "", "#aaa"
    c = int(code)
    if c <= 1:
        if daytime:
            return "☀", "#E65100"       # 快晴・晴れ (deep orange)
        else:
            return "☽", "#FFB300"       # 夜間晴れ — _draw_crescent で描画
    if c <= 60: return "☁", "#37474F"   # 曇り・霧・霧雨 (dark blue-grey)
    return "☔", "#0D47A1"               # 雨・雪・驟雨・雷雨 (dark blue)


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
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(9, 10), sharex=True,
                                        gridspec_kw={"height_ratios": [3, 0.49, 0.98]})
    colors = {"jma_msm": "#d6336c", "jma_gsm": "#f08c00",
              "ecmwf_ifs025": "#1f5fa5", "gfs_seamless": "#2f9e44"}

    # バイアス補正テーブル読み込み (calibration.json があれば適用)
    _cal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    cal = None
    if os.path.exists(_cal_path):
        try:
            with open(_cal_path, encoding="utf-8") as _f:
                cal = json.load(_f)
        except Exception:
            cal = None

    # ── パネル1: 風速 ──────────────────────────────────────────
    t0 = None
    corrected_by_time: dict = {}

    for m in models:
        rows = conn.execute(
            """SELECT valid_time, wind_speed_ms FROM forecasts
               WHERE fetched_at=? AND model=? AND lead_hours<=?
               ORDER BY valid_time""", (fa, m, MAX_LEAD)).fetchall()
        if not rows:
            continue
        t = [_jst_naive(r["valid_time"]) for r in rows]
        ms_raw = [r["wind_speed_ms"] for r in rows]
        t0 = t0 or t[0]

        # 時間帯別バイアス補正
        if cal and m in cal["models"]:
            hb = cal["models"][m]["hourly_bias"]
            ob = cal["models"][m]["bias_overall"]
            wt = cal["models"][m]["weight"]
            ms = []
            for ti, v in zip(t, ms_raw):
                if v is not None:
                    c = max(0.0, v - hb.get(str(ti.hour), ob))
                    ms.append(c)
                    corrected_by_time.setdefault(ti, []).append((wt, c))
                else:
                    ms.append(None)
        else:
            ms = ms_raw

        lw    = (2.0 if m == "jma_msm" else 1.2) if cal else (2.8 if m == "jma_msm" else 1.5)
        alpha = 0.65 if cal else 1.0
        ax1.plot(t, ms, label=m + ("（本命）" if m == "jma_msm" and not cal else ""),
                 color=colors.get(m, "#888"), linewidth=lw, marker="o",
                 markersize=2.5, alpha=alpha)

    # 加重アンサンブル線（補正済み）
    if cal and corrected_by_time:
        ens_t  = sorted(corrected_by_time.keys())
        ens_ms = [sum(w * v for w, v in corrected_by_time[ti]) /
                  sum(w for w, _ in corrected_by_time[ti])
                  for ti in ens_t]
        ax1.plot(ens_t, ens_ms, color="#111", lw=2.8, ls="--",
                 label="加重平均（補正済み）", zorder=6)

    ax1.axhspan(pc.SAIL_MIN_MS, pc.SAIL_MAX_MS, color="#2f9e44", alpha=0.08)
    ax1.axhline(pc.SAIL_MIN_MS, color="#2f9e44", ls="--", lw=1, alpha=.6)
    ax1.axhline(pc.SAIL_MAX_MS, color="#e8590c", ls="--", lw=1, alpha=.6)
    if t0:
        ax1.text(t0, pc.SAIL_MAX_MS + 0.3, "出走レンジ", color="#2f9e44", fontsize=9)
    ax1.set_ylabel("風速 (m/s)")
    ax1.set_ylim(0, max(pc.SAIL_MAX_MS + 2, 16))
    _fa = datetime.fromisoformat(fa).astimezone(pc.JST)
    cal_note = (f"  補正: {cal['obs_period']['start']}~{cal['obs_period']['end']}"
                if cal else "")
    ax1.set_title(f"Ushibuse Beach Multi-model Wind Forecast  (issued {_fa.month}/{_fa.day}"
                  f"({_WD[_fa.weekday()]}) {_fa:%H:%M} JST){cal_note}", fontsize=12)
    ax1.grid(alpha=.25)

    # 凡例: 加重平均を先頭に、個別モデルを後ろに
    handles_dict = {line.get_label(): line for line in ax1.get_lines()}
    ordered_handles, ordered_labels = [], []
    if "加重平均（補正済み）" in handles_dict:
        ordered_handles.append(handles_dict["加重平均（補正済み）"])
        ordered_labels.append("加重平均（補正済み）")
    for m in _LEGEND_ORDER:
        label = m + ("（本命）" if m == "jma_msm" and not cal else "")
        if label in handles_dict:
            ordered_handles.append(handles_dict[label])
            ordered_labels.append(label)
    ax1.legend(ordered_handles, ordered_labels, fontsize=8, loc="upper right",
               ncol=1, framealpha=0.8)

    # 出走判定の対象時刻に縦線
    for vtiso in pc.next_n_clock_valid_times(conn, fa, pc.TARGET_HOURS_JST):
        x = _jst_naive(vtiso)
        hour = datetime.fromisoformat(vtiso).astimezone(pc.JST).hour
        ax1.axvline(x, color="#6741d9", ls=":", lw=1.3, alpha=.7)
        ax1.text(x, 0.04, f"{hour:02d}:00", color="#6741d9",
                 fontsize=8, ha="center", va="bottom",
                 transform=ax1.get_xaxis_transform())

    # ── パネル2: 風向矢印 ──────────────────────────────────────
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
    ax2.set_ylabel("風向\n(jma_msm)", fontsize=11)
    ax2.text(0.005, 0.97, "矢印 = 風の進む向き（上が北）", transform=ax2.transAxes,
             fontsize=8, va="top", color="#555")
    ax2.grid(alpha=.2, axis="x")

    # ── パネル3: 天気・気温・降水（テキスト表形式） ──────────────

    # 全モデルのweather_code・降水量を取得（多数決・中央値計算用）
    all_wx = conn.execute(
        """SELECT valid_time, weather_code, precipitation_mm FROM forecasts
           WHERE fetched_at=? AND lead_hours<=?
           ORDER BY valid_time""", (fa, MAX_LEAD)).fetchall()

    # jma_msmの気温（ローカル精度が高い）
    temp_map = {r["valid_time"]: r["temperature_2m_c"] for r in conn.execute(
        """SELECT valid_time, temperature_2m_c FROM forecasts
           WHERE fetched_at=? AND model='jma_msm' AND lead_hours<=?
           ORDER BY valid_time""", (fa, MAX_LEAD)).fetchall()}

    # valid_time ごとに集約
    from collections import defaultdict
    _wx_codes: dict = defaultdict(list)
    _precip_vals: dict = defaultdict(list)
    for r in all_wx:
        if r["weather_code"] is not None:
            _wx_codes[r["valid_time"]].append(int(r["weather_code"]))
        if r["precipitation_mm"] is not None:
            _precip_vals[r["valid_time"]].append(r["precipitation_mm"])
    valid_times = sorted(set(r["valid_time"] for r in all_wx))

    # y軸: 3行構成(天気=2.5, 気温=1.5, 降水=0.5)
    ax3.set_ylim(0, 3)
    ax3.set_yticks([0.5, 1.5, 2.5])
    ax3.set_yticklabels(["降水量\n(mm/h)", "気温\n(°C)", "天気\n(4モデル)"], fontsize=11)
    ax3.tick_params(axis="y", length=0)
    ax3.axhline(1.0, color="#ddd", lw=0.7)
    ax3.axhline(2.0, color="#ddd", lw=0.7)

    step = 3
    for i, vt in enumerate(valid_times):
        if i % step != 0:
            continue
        x = _jst_naive(vt)

        # 天気シンボル: 4モデル多数決 (y=2.5)
        codes = _wx_codes.get(vt, [])
        n = len(codes)
        n_sunny = sum(1 for c in codes if c <= 1)
        n_rainy = sum(1 for c in codes if c >= 61)
        if n >= 2 and n_sunny * 2 >= n:
            vote_code = 0    # 晴れ多数
        elif n >= 2 and n_rainy * 2 >= n:
            vote_code = 99   # 雨多数
        elif n > 0:
            vote_code = 3    # 曇り
        else:
            vote_code = None
        label, color = _wmo_label(vote_code, daytime=_is_daytime(x))
        if label == "☽":
            _draw_crescent(ax3, x, 2.5, color)
        else:
            ax3.text(x, 2.5, label if label else "--", ha="center", va="center",
                     fontsize=22, color=color, fontproperties=_sym_fp)

        # 気温 (jma_msm、y=1.5)
        temp = temp_map.get(vt)
        t_color = _temp_color(temp)
        ax3.text(x, 1.5, f"{temp:.0f}°" if temp is not None else "--",
                 ha="center", va="center", fontsize=13, color=t_color, fontweight="bold")

        # 降水量: 4モデル中央値 (y=0.5) — 四捨五入後の値で色を決定
        pvals = _precip_vals.get(vt, [])
        precip = float(np.median(pvals)) if pvals else None
        precip_int = round(precip) if precip is not None else None
        precip_txt = str(precip_int) if precip_int is not None else "--"
        color = "#1565C0" if (precip_int or 0) > 0 else "#999"
        ax3.text(x, 0.5, precip_txt, ha="center", va="center",
                 fontsize=13, color=color, fontweight="bold" if (precip_int or 0) > 0 else "normal")

    ax3.grid(alpha=.2, axis="x")
    ax3.xaxis.set_major_formatter(FuncFormatter(_fmt_date))

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
    # 旧DBに新カラムがない場合のマイグレーション
    for sql in ("ALTER TABLE forecasts ADD COLUMN weather_code INTEGER",
                "ALTER TABLE forecasts ADD COLUMN precipitation_prob REAL",
                "ALTER TABLE forecasts ADD COLUMN precipitation_mm REAL"):
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()
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
