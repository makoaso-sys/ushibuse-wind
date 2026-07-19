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


def load_obs_db(db_path: str) -> pd.DataFrame:
    """fetch_weathercloud.py が作る observations.db (毎時UTC) を読み込む。"""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        "SELECT valid_time, wind_speed_ms AS spd, wind_dir_deg AS dir FROM observations",
        conn, parse_dates=["valid_time"])
    conn.close()
    df = df.set_index("valid_time").sort_index()   # 既に毎時UTC naive
    return df[["spd", "dir"]]


def load_obs_sources(csv_paths: list[str], db_paths: list[str]) -> pd.DataFrame:
    """CSV(履歴)と observations.db(継続)を結合。時刻重複はDB側を優先する。"""
    frames = [load_obs(p) for p in csv_paths] + [load_obs_db(p) for p in db_paths]
    if not frames:
        raise SystemExit("観測ソースが指定されていません (--obs / --obs-db)")
    # 後勝ち(db_paths が csv_paths の後ろ)で重複時刻を解決
    obs = pd.concat(frames)
    obs = obs[~obs.index.duplicated(keep="last")].sort_index()
    return obs


N_HARM = 2          # 日周バイアスの調和次数。時刻別平均24個より自由度が少なく安定
WEIGHT_FLOOR_MS = 0.5   # 強風重視の重み下限。弱風も0にはしない
MAX_LEAD_FIT = 24       # 重み・バイアスを当てはめる lead 範囲(検証した範囲)


def _harm(hour: np.ndarray) -> np.ndarray:
    """時刻 -> 調和関数の計画行列(定数 + sin/cos を N_HARM 次まで)。"""
    a = 2 * np.pi * np.asarray(hour, float) / 24.0
    cols = [np.ones(len(a))]
    for k in range(1, N_HARM + 1):
        cols += [np.sin(k * a), np.cos(k * a)]
    return np.column_stack(cols)


def fit_hourly_bias(hour: np.ndarray, err: np.ndarray,
                    w: np.ndarray) -> dict[str, float]:
    """日周バイアスを調和回帰で推定し、0〜23時の値として返す。

    時刻別の単純平均だと1時刻あたりの標本が少なく暴れる。滑らかな関数に
    当てはめてから各時刻を評価することで、同じ JSON 形式のまま安定させる。
    """
    H, sw = _harm(hour), np.sqrt(w)
    beta, *_ = np.linalg.lstsq(H * sw[:, None], err * sw, rcond=None)
    fitted = _harm(np.arange(24)) @ beta
    return {str(h): round(float(fitted[h]), 4) for h in range(24)}


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """ベクトルを確率単体(非負・総和1)へユークリッド射影する。"""
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - 1.0
    idx = np.arange(1, len(v) + 1)
    cond = u - css / idx > 0
    rho = idx[cond][-1]
    return np.maximum(v - css[cond][-1] / rho, 0.0)


def fit_weights(X: np.ndarray, y: np.ndarray, w: np.ndarray,
                iters: int = 3000) -> np.ndarray:
    """非負・総和1に制約した重みを射影勾配法で求める(scipy 非依存)。

    制約なしの最小二乗だと負の重みや極端な外挿が出て、標本が少ないうちは
    等重み平均にも負ける。制約を課すと自由度が モデル数-1 まで落ち、
    最悪でも等重み平均の近傍に留まる。
    """
    n = X.shape[1]
    b = np.full(n, 1.0 / n)
    Xw = X * w[:, None]
    A, c = X.T @ Xw, Xw.T @ y
    lr = 1.0 / (np.linalg.norm(A, 2) + 1e-9)
    for _ in range(iters):
        b = _project_simplex(b - lr * (A @ b - c))
    return b


def circ_diff(a: pd.Series, b: pd.Series) -> pd.Series:
    """円環差分 (a - b) を [-180, 180) に丸める。"""
    return ((a - b) + 180) % 360 - 180


def calibrate(obs: pd.DataFrame, db_path: str = DB_PATH, out_path: str = OUT_PATH,
              obs_sources: list[str] | None = None) -> None:
    print(f"観測データ: {len(obs)} 時間点  ({obs.index[0].date()} ~ {obs.index[-1].date()} UTC)")

    conn = sqlite3.connect(db_path)
    fcast = pd.read_sql(
        # lead>=0 の「本物の予測」のみで補正する(過去hourのナウキャスト相当は除外)
        "SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg "
        "FROM forecasts WHERE lead_hours >= 0 ORDER BY model, valid_time",
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
        "obs_path": obs_sources or [],
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
        g = g.copy()
        g["hour_jst"] = ((g["valid_time"] + pd.Timedelta(hours=9)).dt.hour)
        valid_dir = g.dropna(subset=["wind_dir_deg", "obs_dir"])
        dir_bias = float(circ_diff(valid_dir["wind_dir_deg"], valid_dir["obs_dir"]).mean()) \
                   if len(valid_dir) else 0.0

        # JST 時間帯別風速バイアス。実測風速で重み付けし(強風域を重視)、
        # 調和回帰で滑らかにしてから各時刻を評価する。
        hourly_n: dict[str, int] = {
            str(int(h)): int(len(hg)) for h, hg in g.groupby("hour_jst")}
        fit = g[g["lead_hours"] <= MAX_LEAD_FIT]
        hourly_bias = fit_hourly_bias(
            fit["hour_jst"].values,
            (fit["wind_speed_ms"] - fit["obs_spd"]).values,
            np.clip(fit["obs_spd"].values, WEIGHT_FLOOR_MS, None),
        )

        # JST 時間帯別風向バイアス
        hourly_dir_bias: dict[str, float] = {}
        for h, hg in valid_dir.groupby("hour_jst"):
            hd = circ_diff(hg["wind_dir_deg"], hg["obs_dir"])
            hourly_dir_bias[str(int(h))] = round(float(hd.mean()), 2)

        result["models"][model] = {
            "rmse":             round(rmse, 4),
            "mae":              round(mae,  4),
            "bias_overall":     round(bias, 4),
            "dir_bias":         round(dir_bias, 2),
            "hourly_bias":      hourly_bias,
            "hourly_dir_bias":  hourly_dir_bias,
            "hourly_n":         hourly_n,
        }
        print(f"  {model:20s}: bias={bias:+.3f} m/s  RMSE={rmse:.3f}  dir_bias={dir_bias:+.1f}°")

    # --- アンサンブル重み ---
    # バイアス補正「後」の予測に対して、非負・総和1の制約付きで当てはめる。
    # 補正前に重みを決めると「技術の高いモデル」ではなく「バイアスの小さい
    # モデル」を選んでしまう。この2段構えで初めて等重み平均を上回る
    # (4週間の前向き検証で 0.968 -> 0.927 m/s、+4.3%)。
    models = sorted(result["models"])
    piv = merged[merged["lead_hours"] <= MAX_LEAD_FIT].copy()
    piv["hour_jst"] = (piv["valid_time"] + pd.Timedelta(hours=9)).dt.hour
    for m in models:                       # 各モデルを自身の日周バイアスで補正
        hb = result["models"][m]["hourly_bias"]
        sel = piv["model"] == m
        piv.loc[sel, "wind_speed_ms"] -= piv.loc[sel, "hour_jst"].map(
            lambda h: hb[str(int(h))]).astype(float)
    wide = piv.pivot_table(index="valid_time", columns="model",
                           values="wind_speed_ms", observed=True)
    obs_w = piv.groupby("valid_time")["obs_spd"].first()
    both = wide.join(obs_w).dropna(subset=models + ["obs_spd"])

    if len(both) >= 100:
        weights = fit_weights(
            both[models].clip(lower=0).values, both["obs_spd"].values,
            np.clip(both["obs_spd"].values, WEIGHT_FLOOR_MS, None))
    else:   # 標本が足りないうちは等重み。無理に最適化しても過学習するだけ。
        print(f"  (照合 {len(both)} 件では重み推定に不足。等重みを使う)")
        weights = np.full(len(models), 1.0 / len(models))

    for m, wv in zip(models, weights):
        result["models"][m]["weight"] = round(float(wv), 4)
    result["weight_method"] = ("simplex_on_bias_corrected"
                               if len(both) >= 100 else "equal")
    result["weight_n"] = int(len(both))

    print("\n=== アンサンブル重み (バイアス補正後・非負総和1) ===")
    for m, v in sorted(result["models"].items(), key=lambda x: -x[1]["weight"]):
        print(f"  {m:20s}: {v['weight']:.3f}  (RMSE {v['rmse']:.3f} m/s)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n保存完了: {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="牛臥海岸 バイアス補正係数生成")
    ap.add_argument("--obs", action="append", default=[],
                    help="WeatherCloud CSV パス(複数指定可・履歴用)")
    ap.add_argument("--obs-db", action="append", default=[],
                    help="observations.db パス(複数指定可・継続取得用)")
    ap.add_argument("--db",  default=DB_PATH,  help="wind.db パス")
    ap.add_argument("--out", default=OUT_PATH, help="出力 JSON パス")
    args = ap.parse_args()
    if not args.obs and not args.obs_db:
        ap.error("--obs か --obs-db を少なくとも1つ指定してください")
    obs = load_obs_sources(args.obs, args.obs_db)
    calibrate(obs, args.db, args.out, obs_sources=args.obs + args.obs_db)


if __name__ == "__main__":
    main()
