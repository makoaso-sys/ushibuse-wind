#!/usr/bin/env python3
"""
観測データとDBの予測を比較し、時間帯別バイアス補正テーブルを生成する。

使い方:
  python calibrate.py --obs ../measured/Weathercloud\ BreezePlay\ Ushibuse\ 2026-06.csv
  python calibrate.py --obs ../measured/Weathercloud\ BreezePlay\ Ushibuse\ 2026-06.csv --db wind.db

出力:
  calibration.json  -- generate_dashboard.py が自動読み込みするバイアス補正テーブル
                       このファイルをgitにコミットするとGitHub Actionsでも補正が適用される。

補正内容:
  風速: 各モデルのJST時間帯別バイアス (model_forecast - obs の平均) を引く
  アンサンブル: RMSE逆数で重み付けした加重平均
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB_PATH  = "wind.db"
OUT_PATH = "calibration.json"


def load_obs(csv_path: str) -> pd.DataFrame:
    """Weathercloud CSV (Asia/Tokyo) を読み込み、UTC 1時間平均を返す。"""
    df = pd.read_csv(csv_path, sep=";", encoding="utf-16-le", on_bad_lines="skip")
    df["time"] = pd.to_datetime(df["Date (Asia/Tokyo)"], format="%d/%m/%Y %H:%M:%S")
    df = df.set_index("time")
    df["spd"] = pd.to_numeric(df["Average wind speed (m/s)"], errors="coerce")
    df["dir"] = pd.to_numeric(df["Average wind direction (°)"], errors="coerce")
    # ベクトル平均で風向を保持しながら1時間平均
    df["u"] = -df["spd"] * np.sin(np.radians(df["dir"]))
    df["v"] = -df["spd"] * np.cos(np.radians(df["dir"]))
    obs_h = df[["spd", "u", "v"]].resample("1h").mean()
    obs_h["dir"] = (np.degrees(np.arctan2(-obs_h["u"], -obs_h["v"])) % 360)
    obs_h.index = obs_h.index - pd.Timedelta(hours=9)   # JST -> UTC (naive)
    return obs_h[["spd", "dir"]]


def circ_diff(a: pd.Series, b: pd.Series) -> pd.Series:
    """円環差分 (a - b) を [-180, 180) に丸める。"""
    return ((a - b) + 180) % 360 - 180


def calibrate(obs_path: str, db_path: str = DB_PATH, out_path: str = OUT_PATH) -> None:
    obs = load_obs(obs_path)
    print(f"観測データ: {len(obs)} 時間点  ({obs.index[0].date()} ~ {obs.index[-1].date()} UTC)")

    conn = sqlite3.connect(db_path)
    fcast = pd.read_sql(
        "SELECT model, valid_time, wind_speed_ms, wind_dir_deg FROM forecasts ORDER BY model, valid_time",
        conn)
    conn.close()

    fcast["valid_time"] = pd.to_datetime(fcast["valid_time"]).dt.tz_localize(None)
    merged = fcast.merge(
        obs.rename(columns={"spd": "obs_spd", "dir": "obs_dir"}),
        left_on="valid_time", right_index=True, how="inner",
    ).dropna(subset=["obs_spd", "wind_speed_ms"])

    print(f"マッチング: {len(merged)} 件\n")

    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "obs_path": obs_path,
        "obs_period": {
            "start": str(obs.index[0].date()),
            "end":   str(obs.index[-1].date()),
        },
        "n_matches": len(merged),
        "models": {},
    }

    for model, g in merged.groupby("model"):
        spd_err = g["wind_speed_ms"] - g["obs_spd"]
        rmse  = float(np.sqrt((spd_err ** 2).mean()))
        mae   = float(spd_err.abs().mean())
        bias  = float(spd_err.mean())

        # 風向バイアス (欠損除く)
        valid_dir = g.dropna(subset=["wind_dir_deg", "obs_dir"])
        dir_bias = float(circ_diff(valid_dir["wind_dir_deg"], valid_dir["obs_dir"]).mean()) \
                   if len(valid_dir) else 0.0

        # JST 時間帯別バイアス
        g = g.copy()
        g["hour_jst"] = ((g["valid_time"] + pd.Timedelta(hours=9)).dt.hour)
        hourly_bias: dict[str, float] = {}
        hourly_n:    dict[str, int]   = {}
        for h, hg in g.groupby("hour_jst"):
            he = hg["wind_speed_ms"] - hg["obs_spd"]
            hourly_bias[str(int(h))] = round(float(he.mean()), 4)
            hourly_n[str(int(h))]    = int(len(he))

        result["models"][model] = {
            "rmse":         round(rmse, 4),
            "mae":          round(mae,  4),
            "bias_overall": round(bias, 4),
            "dir_bias":     round(dir_bias, 2),
            "hourly_bias":  hourly_bias,
            "hourly_n":     hourly_n,
        }
        print(f"  {model:20s}: bias={bias:+.3f} m/s  RMSE={rmse:.3f}  dir_bias={dir_bias:+.1f}°")

    # RMSE 逆数による重み (全モデル合計=1)
    rmses   = {m: v["rmse"] for m, v in result["models"].items()}
    inv_sum = sum(1 / r for r in rmses.values())
    for m in result["models"]:
        result["models"][m]["weight"] = round(1 / rmses[m] / inv_sum, 4)

    print("\n=== アンサンブル重み ===")
    for m, v in sorted(result["models"].items(), key=lambda x: -x[1]["weight"]):
        print(f"  {m:20s}: {v['weight']:.3f}  (RMSE {v['rmse']:.3f} m/s)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n保存完了: {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="牛臥海岸 バイアス補正係数生成")
    ap.add_argument("--obs", required=True, help="WeatherCloud CSV パス")
    ap.add_argument("--db",  default=DB_PATH,  help="wind.db パス")
    ap.add_argument("--out", default=OUT_PATH, help="出力 JSON パス")
    args = ap.parse_args()
    calibrate(args.obs, args.db, args.out)


if __name__ == "__main__":
    main()
