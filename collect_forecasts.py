#!/usr/bin/env python3
"""
牛臥海岸 風予測システム — フェーズ1: 気象モデル取得パイプライン
================================================================

複数の気象モデル(JMA MSM/GSM, ECMWF IFS/AIFS, GFS ...)の 12〜24 時間先予測を
Open-Meteo から定期取得し、SQLite に「縦持ち」(1予測点=1行)で蓄積する。

設計上の要点:
  * fetched_at(取得時刻=UTC)を発表時刻の代理として必ず記録する。
    Open-Meteo の通常 forecast API はモデルの初期時刻(init)を返さないため、
    リーク防止の基準には「取得時点で実際に手に入っていた」= fetched_at を使う。
    lead_hours = valid_time - fetched_at。これは保守的(過去の情報を先取りしない)。
  * モデルは config の MODELS で増減できる。1モデル=1リクエストにして、
    応答キーが素直(suffix なし)になるようにし、失敗を1モデルに閉じ込める。
  * 風速・風向はその場で u(東向き)・v(北向き)成分に分解して保存する。

使い方:
  python collect_forecasts.py            # 実際に取得して保存
  python collect_forecasts.py --demo     # ネット不要。合成データで一連の流れを検証
  python collect_forecasts.py --db /path/to/wind.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import requests

# ============================================================
# 設定(ここを編集する)
# ============================================================

# 牛臥海岸のおおよその座標。MSM は 5km 格子なので厳密でなくてよいが、
# 必要なら地図で確認して微調整すること。
LATITUDE = 35.074094
LONGITUDE = 138.868262
LOCATION_NAME = "Ushibuse Beach, Numazu"

# 取得するモデル(Open-Meteo の model 識別子)。
# 確実に動く定番から始め、AI 系は識別子が変わりやすいので任意で有効化する。
# 応答が空のモデルは自動でスキップしログに残るので、無効な ID があっても落ちない。
MODELS = [
    "jma_msm",        # 気象庁メソモデル 5km(日本特化・本命)
    "jma_gsm",        # 気象庁全球モデル
    "ecmwf_ifs025",   # ECMWF IFS 0.25°
    "gfs_seamless",   # NOAA GFS
    # --- 任意(識別子が変わることがある。動かなければコメントアウトのまま) ---
    # "ecmwf_aifs025",   # ECMWF の AI モデル AIFS
    # "gfs_graphcast025",# GFS GraphCast
]

# 取得する気象変数(Open-Meteo hourly 変数名)
HOURLY_VARS = [
    "wind_speed_10m",
    "wind_direction_10m",
    "surface_pressure",
    "temperature_2m",
    "weather_code",
    "precipitation_probability",
    "precipitation",
]

FORECAST_DAYS = 3          # 12〜24h をカバーするのに十分(MSM の地平線にも収まる)
WIND_SPEED_UNIT = "ms"     # m/s で取得(u/v 計算が素直。1kt = 0.514 m/s)
DB_PATH = "wind.db"
API_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 30          # seconds


# ============================================================
# 風の u/v 変換(気象慣習: 風向 = 風が吹いてくる向き)
# ============================================================

def deg_to_uv(speed: float, dir_deg: float) -> tuple[float, float]:
    """風速・風向 -> u(東向き), v(北向き)成分。"""
    r = math.radians(dir_deg)
    u = -speed * math.sin(r)
    v = -speed * math.cos(r)
    return u, v


# ============================================================
# SQLite
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    model                TEXT    NOT NULL,
    fetched_at           TEXT    NOT NULL,
    valid_time           TEXT    NOT NULL,
    lead_hours           REAL    NOT NULL,
    wind_speed_ms        REAL,
    wind_dir_deg         REAL,
    wind_u               REAL,
    wind_v               REAL,
    surface_pressure_hpa REAL,
    temperature_2m_c     REAL,
    weather_code         INTEGER,
    precipitation_prob   REAL,
    precipitation_mm     REAL,
    latitude             REAL,
    longitude            REAL,
    UNIQUE(model, fetched_at, valid_time)
);
CREATE INDEX IF NOT EXISTS idx_valid      ON forecasts(valid_time);
CREATE INDEX IF NOT EXISTS idx_model_valid ON forecasts(model, valid_time);
CREATE INDEX IF NOT EXISTS idx_lead       ON forecasts(model, lead_hours);
"""

_MIGRATIONS = [
    "ALTER TABLE forecasts ADD COLUMN weather_code INTEGER",
    "ALTER TABLE forecasts ADD COLUMN precipitation_prob REAL",
    "ALTER TABLE forecasts ADD COLUMN precipitation_mm REAL",
]

INSERT_SQL = """
INSERT OR IGNORE INTO forecasts
    (model, fetched_at, valid_time, lead_hours,
     wind_speed_ms, wind_dir_deg, wind_u, wind_v,
     surface_pressure_hpa, temperature_2m_c,
     weather_code, precipitation_prob, precipitation_mm,
     latitude, longitude)
VALUES
    (:model, :fetched_at, :valid_time, :lead_hours,
     :wind_speed_ms, :wind_dir_deg, :wind_u, :wind_v,
     :surface_pressure_hpa, :temperature_2m_c,
     :weather_code, :precipitation_prob, :precipitation_mm,
     :latitude, :longitude)
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    cur = conn.executemany(INSERT_SQL, rows)
    conn.commit()
    return cur.rowcount


# ============================================================
# 取得とパース
# ============================================================

def parse_utc(t: str) -> datetime:
    """Open-Meteo の '2026-06-20T06:00' を UTC aware datetime にする。"""
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc)


def _get(seq, i):
    return seq[i] if seq and i < len(seq) and seq[i] is not None else None


def parse_payload(model: str, fetched_at: datetime, payload: dict) -> list[dict]:
    """Open-Meteo の単一モデル応答を行リストに変換する(lead>=0 のみ)。"""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    speed = hourly.get("wind_speed_10m") or []
    wdir = hourly.get("wind_direction_10m") or []
    pres = hourly.get("surface_pressure") or []
    temp = hourly.get("temperature_2m") or []
    wcode = hourly.get("weather_code") or []
    prob = hourly.get("precipitation_probability") or []
    precip = hourly.get("precipitation") or []

    rows: list[dict] = []
    for i, t in enumerate(times):
        vt = parse_utc(t)
        lead = (vt - fetched_at).total_seconds() / 3600.0
        if lead < 0:                      # 過去時刻は捨てる(将来予測のみ蓄積)
            continue
        sp = _get(speed, i)
        di = _get(wdir, i)
        u = v = None
        if sp is not None and di is not None:
            u, v = deg_to_uv(sp, di)
        rows.append({
            "model": model,
            "fetched_at": fetched_at.isoformat(),
            "valid_time": vt.isoformat(),
            "lead_hours": round(lead, 2),
            "wind_speed_ms": sp,
            "wind_dir_deg": di,
            "wind_u": round(u, 4) if u is not None else None,
            "wind_v": round(v, 4) if v is not None else None,
            "surface_pressure_hpa": _get(pres, i),
            "temperature_2m_c": _get(temp, i),
            "weather_code": _get(wcode, i),
            "precipitation_prob": _get(prob, i),
            "precipitation_mm": _get(precip, i),
            "latitude": payload.get("latitude", LATITUDE),
            "longitude": payload.get("longitude", LONGITUDE),
        })
    return rows


def fetch_model(model: str, fetched_at: datetime,
                session: requests.Session) -> list[dict]:
    """1モデル分を取得してパース。失敗時は空リストを返す(全体は止めない)。"""
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": ",".join(HOURLY_VARS),
        "models": model,
        "forecast_days": FORECAST_DAYS,
        "wind_speed_unit": WIND_SPEED_UNIT,
        "timezone": "UTC",
    }
    try:
        resp = session.get(API_URL, params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [{model}] リクエスト失敗: {e}", file=sys.stderr)
        return []

    if resp.status_code != 200:
        print(f"  [{model}] HTTP {resp.status_code}: {resp.text[:160]}",
              file=sys.stderr)
        return []
    payload = resp.json()
    if payload.get("error"):
        print(f"  [{model}] API エラー: {payload.get('reason')}", file=sys.stderr)
        return []
    return parse_payload(model, fetched_at, payload)


# ============================================================
# デモ用合成応答(ネット不要・パイプライン検証用)
# ============================================================

def demo_payload(model: str, fetched_at: datetime) -> dict:
    """48時間ぶんのそれらしい海陸風サイクルを合成する。"""
    # 当日 00:00 UTC から開始(Open-Meteo は time が 0:00 始まり)
    start = fetched_at.replace(hour=0, minute=0, second=0, microsecond=0)
    times, speed, wdir, pres, temp = [], [], [], [], []
    # モデルごとに少しバイアスを付けて差を演出
    bias = {"jma_msm": 0.0, "jma_gsm": 0.8, "ecmwf_ifs025": -0.5,
            "gfs_seamless": 0.4}.get(model, 0.2)
    for h in range(FORECAST_DAYS * 24):
        ts = start + timedelta(hours=h)
        hod = ts.hour
        # 日中(JST)に強まる海風(南寄り)、夜は弱い陸風(北寄り)の簡易モデル
        # JST 13時ごろ(=04 UTC)にピークが来るよう位相を設定
        sea = max(0.0, math.sin((hod + 2) / 24 * 2 * math.pi))
        sp = round(2.5 + 6.0 * sea + bias, 2)
        di = 200.0 if sea > 0.2 else 20.0          # 日中=南南西, 夜=北北東
        times.append(ts.strftime("%Y-%m-%dT%H:%M"))
        speed.append(sp)
        wdir.append(di)
        pres.append(round(1013.0 - 2 * sea, 1))
        temp.append(round(18.0 + 6 * sea, 1))
    return {
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "hourly": {
            "time": times, "wind_speed_10m": speed,
            "wind_direction_10m": wdir, "surface_pressure": pres,
            "temperature_2m": temp,
        },
    }


# ============================================================
# メイン
# ============================================================

def collect(db_path: str, demo: bool = False) -> None:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    conn = init_db(db_path)
    session = requests.Session()
    session.headers.update({"User-Agent": "ushibuse-wind-collector/0.1"})

    print(f"=== 取得開始 {fetched_at.isoformat()} "
          f"({'DEMO' if demo else 'LIVE'}) ===")
    total_new = 0
    for model in MODELS:
        if demo:
            rows = parse_payload(model, fetched_at, demo_payload(model, fetched_at))
        else:
            rows = fetch_model(model, fetched_at, session)
        if not rows:
            print(f"  [{model}] データなし(スキップ)")
            continue
        new = insert_rows(conn, rows)
        total_new += new
        leads = [r["lead_hours"] for r in rows]
        print(f"  [{model}] {len(rows)}点取得 / {new}件新規 "
              f"(lead {min(leads):.0f}〜{max(leads):.0f}h)")

    print(f"=== 完了: 新規 {total_new} 件 -> {db_path} ===")
    _print_targets(conn, fetched_at)
    conn.close()


def _print_targets(conn: sqlite3.Connection, fetched_at: datetime) -> None:
    """12h / 24h 近傍の予測を確認表示する(各モデルで lead に最も近い行)。"""
    print("\n--- 直近取得分の 12h / 24h 予測(各モデル lead 最近傍)---")
    for target in (12, 24):
        print(f"[+{target}h]")
        q = """
        SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg
        FROM forecasts
        WHERE fetched_at = ?
        GROUP BY model
        HAVING MIN(ABS(lead_hours - ?))
        ORDER BY model
        """
        for row in conn.execute(q, (fetched_at.isoformat(), target)):
            model, vt, lead, sp, di = row
            sp_kn = sp * 1.94384 if sp is not None else None
            kn = f"{sp_kn:4.1f}kt" if sp_kn is not None else "  -  "
            print(f"   {model:14s} {vt}  lead={lead:5.1f}h  "
                  f"{sp:4.1f}m/s({kn})  {di:5.0f}°")


def main() -> None:
    ap = argparse.ArgumentParser(description="牛臥海岸 風予測 フェーズ1 取得")
    ap.add_argument("--db", default=DB_PATH, help="SQLite ファイルパス")
    ap.add_argument("--demo", action="store_true",
                    help="ネット不要の合成データで動作確認する")
    args = ap.parse_args()
    collect(args.db, demo=args.demo)


if __name__ == "__main__":
    main()
