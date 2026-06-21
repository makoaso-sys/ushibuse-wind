import sqlite3
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
import numpy as np
import phase6_common as pc

# 日本語フォント
plt.rcParams["font.family"] = "Noto Sans CJK JP"
plt.rcParams["axes.unicode_minus"] = False

# 日付ラベル: 6/21(日) のように曜日を付ける(JST)
_WD = ["月", "火", "水", "木", "金", "土", "日"]   # Mon..Sun = weekday() 0..6
def _fmt_date(x, pos=None):
    d = mdates.num2date(x)
    return f"{d.month}/{d.day}({_WD[d.weekday()]})\n{d:%H:%M}"

MS_TO_KN = 1.94384
# 出走レンジは phase6_common と共通(変更はあちら1か所でよい)
SAIL_MIN_MS = pc.SAIL_MIN_MS
SAIL_MAX_MS = pc.SAIL_MAX_MS

con = sqlite3.connect("wind.db")
fa = con.execute("SELECT MAX(fetched_at) FROM forecasts").fetchone()[0]
models = [r[0] for r in con.execute(
    "SELECT DISTINCT model FROM forecasts WHERE fetched_at=? ORDER BY model", (fa,))]

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6.4), sharex=True,
                               gridspec_kw={"height_ratios": [3, 1]})
colors = {"jma_msm": "#d6336c", "jma_gsm": "#f08c00",
          "ecmwf_ifs025": "#1f5fa5", "gfs_seamless": "#2f9e44"}

for m in models:
    rows = con.execute("""SELECT valid_time, wind_speed_ms FROM forecasts
                          WHERE fetched_at=? AND model=? AND lead_hours<=48
                          ORDER BY valid_time""", (fa, m)).fetchall()
    t = [datetime.fromisoformat(r[0]).astimezone(pc.JST).replace(tzinfo=None)
         for r in rows]
    ms = [r[1] for r in rows]
    lw = 2.8 if m == "jma_msm" else 1.5
    ax1.plot(t, ms, label=m + ("（本命）" if m == "jma_msm" else ""),
             color=colors.get(m, "#888"), linewidth=lw, marker="o", markersize=2.5)

# 出走レンジの帯(m/s)
ax1.axhspan(SAIL_MIN_MS, SAIL_MAX_MS, color="#2f9e44", alpha=0.08)
ax1.axhline(SAIL_MIN_MS, color="#2f9e44", ls="--", lw=1, alpha=.6)
ax1.axhline(SAIL_MAX_MS, color="#e8590c", ls="--", lw=1, alpha=.6)
ax1.text(t[0], SAIL_MAX_MS + 0.3, "出走レンジ", color="#2f9e44", fontsize=9)
ax1.set_ylabel("風速 (m/s)")
ax1.set_ylim(0, max(SAIL_MAX_MS + 2, 16))
_fa = datetime.fromisoformat(fa).astimezone(pc.JST)
ax1.set_title(f"牛臥海岸 複数モデル風予測（発表 {_fa.month}/{_fa.day}"
              f"({_WD[_fa.weekday()]}) {_fa:%H:%M} JST）", fontsize=12)
ax1.legend(fontsize=8, ncol=4, loc="upper right")
ax1.grid(alpha=.25)

# 風向パネル: 一定長・中央ピボットの矢印(見切れない)
rows = con.execute("""SELECT valid_time, wind_u, wind_v FROM forecasts
                      WHERE fetched_at=? AND model='jma_msm' AND lead_hours<=48
                      ORDER BY valid_time""", (fa,)).fetchall()
step = 3
t = [datetime.fromisoformat(r[0]).astimezone(pc.JST).replace(tzinfo=None)
     for r in rows][::step]
u = np.array([r[1] for r in rows][::step])   # wind_u,wind_v = 風が進む向きの成分
v = np.array([r[2] for r in rows][::step])
mag = np.hypot(u, v)
mag[mag == 0] = 1.0
u_unit, v_unit = u / mag, v / mag            # 単位ベクトル化(長さを揃える)

ax2.axhline(0, color="#ccc", lw=0.8)
ax2.quiver(t, np.zeros(len(t)), u_unit, v_unit,
           color="#1f5fa5", angles="uv", scale_units="inches", scale=2.2,
           width=0.004, headwidth=4, headlength=5, pivot="mid")
ax2.set_ylim(-1, 1)
ax2.set_yticks([])
ax2.set_ylabel("風向\n(jma_msm)", fontsize=9)
ax2.text(0.005, 0.97, "矢印 = 風の進む向き（上=北）", transform=ax2.transAxes,
         fontsize=8, va="top", color="#555")
ax2.xaxis.set_major_formatter(FuncFormatter(_fmt_date))
ax2.grid(alpha=.2, axis="x")

plt.tight_layout()
plt.savefig("sample_dashboard.png", dpi=110)
print("saved")
