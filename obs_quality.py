#!/usr/bin/env python3
"""観測値のもっともらしさを判定し、ありえない値を落とす。

牛臥の Weathercloud は稀に単発のスパイクを出す。2026-05-31〜07-27 の 1312 時間点で
実際に混入していたもの:

    気温   2026-06-06 22:00 UTC  -38.1°C   (前後は 19.1 / 20.5)
           2026-07-25 14:00 UTC  +60.0°C   (前後は 24.6 / 24.6)
    突風   8件。平均 0.0〜1.9 m/s に対して突風 15.4〜35.2 m/s

これらはダッシュボードの実測オーバーレイにそのまま出るうえ、バイアス補正
(calibrate.py)の学習データも汚す。取り込み時(fetch_weathercloud.store)と
表示時(generate_dashboard._load_obs)の両方でここを通す。

外れた項目だけを None にし、行ごとは捨てない。同じ時刻の他の項目は正常なこと
が多く(60.0°C の行も風は正常だった)、下流は None を「観測なし」として既に
扱えるため。

    python obs_quality.py check observations.db    # 落とす対象を表示するだけ
    python obs_quality.py clean observations.db    # 既存DBの異常値を None にする
"""
from __future__ import annotations

import sqlite3
import sys

# 物理的にありえない値だけを弾く広い枠。観測を絞り込むためのものではない。
#
# 気温は気候値からの判断で、観測から当てたものではない(手持ちデータは夏だけで
# 実績 16.7〜33.4°C しかなく、冬を含むと下限はもっと下がる)。沼津の記録は概ね
# -7〜39°C なので、そこから更に余裕を取った。実際の異常値 -38.1 / +60.0 とは
# 大きく離れており、本物の気象を削る心配はない。
#
# 風速・気圧・湿度の枠はもっと粗い。ここに引っかかるのは 9999 のような
# センサの欠測値・破損値だけで、実データでは一件も該当しない(=普段は効かない)。
LIMITS: dict[str, tuple[float, float]] = {
    "temp_c":        (-15.0, 45.0),
    "humidity":      (0.0, 100.0),
    "pressure_hpa":  (900.0, 1080.0),
    "wind_speed_ms": (0.0, 60.0),
    "wind_gust_ms":  (0.0, 80.0),
    "wind_dir_deg":  (0.0, 360.0),
}

# 突風の整合性。毎時平均が無風に近いのに突風だけ大きい値はセンサのグリッチで、
# 上の絶対値の枠では捕まらない(35.2 m/s は「ありえない値」ではない)。
#
# 閾値は実測 1312 点で確かめた。どちらも観測値の空白域に置いてある:
#
#   絶対値 GUST_MIN_MS=10
#     弱風時のガストファクタは当てにならない(平均 0.02 m/s で比 42 でも突風は
#     0.7 m/s＝無害)。ただしその手の値は突風 3.2 m/s 以下に収まる。一方で
#     グリッチは最小でも 15.4 m/s。3.2 と 15.4 の間なのでどこでもよいが、
#     切りのいい 10 にした。
#
#   比 GUST_RATIO=5
#     突風 10 m/s 超のうち、前後2時間も強く実在が確かなものの比は最大 4.0
#     (06/05 02:00 平均3.72→突風14.9)。単発スパイクの比は最小 8.3
#     (07/16 01:00 平均1.85→突風15.4)。4.0 と 8.3 の間を取った。
#
# この2つで、単発スパイク8件を全て落とし、突風 10 m/s 超の実在15件
# (平均 3.7〜8.1 m/s の強風時)を全て残せることを確認済み。
GUST_MIN_MS = 10.0
GUST_RATIO = 5.0


def sanitize(row: dict) -> tuple[dict, list[str]]:
    """観測1行を検査する。 -> (掃除済みの行, 落とした項目の説明)"""
    out = dict(row)
    dropped: list[str] = []

    for key, (lo, hi) in LIMITS.items():
        v = out.get(key)
        if v is not None and not (lo <= v <= hi):
            dropped.append(f"{key}={v:g} (範囲 {lo:g}〜{hi:g} 外)")
            out[key] = None

    spd, gust = out.get("wind_speed_ms"), out.get("wind_gust_ms")
    if spd is not None and gust is not None:
        if gust > GUST_MIN_MS and gust > GUST_RATIO * spd:
            dropped.append(f"wind_gust_ms={gust:g} (平均 {spd:g} m/s に対し過大)")
            out["wind_gust_ms"] = None
        elif gust < spd:
            # 突風は平均風速以上のはず。集計の取りこぼしを補正する
            out["wind_gust_ms"] = spd

    return out, dropped


# ============================================================
# 既存DBの掃除(一度だけ流す)
# ============================================================

COLS = ["wind_speed_ms", "wind_dir_deg", "wind_gust_ms",
        "temp_c", "humidity", "pressure_hpa"]


def clean_db(db_path: str, apply: bool) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT valid_time, {', '.join(COLS)} FROM observations ORDER BY valid_time"
    ).fetchall()

    updates, n = [], 0
    for r in rows:
        clean, dropped = sanitize({c: r[c] for c in COLS})
        if not dropped:
            continue
        n += 1
        print(f"  {r['valid_time']}  " + " / ".join(dropped))
        updates.append([clean[c] for c in COLS] + [r["valid_time"]])

    if apply and updates:
        conn.executemany(
            f"UPDATE observations SET {', '.join(c + '=?' for c in COLS)} "
            f"WHERE valid_time=?", updates)
        conn.commit()
    conn.close()

    print(f"{n} 行が{'掃除された' if apply else '掃除の対象'} / 全 {len(rows)} 行")
    return n


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "clean"):
        sys.exit(__doc__)
    clean_db(sys.argv[2], apply=sys.argv[1] == "clean")
