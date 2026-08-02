#!/usr/bin/env python3
"""
フェーズ6 共通ロジック — 取得済み予測(wind.db)を読み、出走可否を判定する。

dashboard.py と notify.py の両方から使う。
判定関数 evaluate_window() は「差し替え可能」に作ってある:
  * 現在(フェーズ1のみ): 複数モデルの生予測の合議で判定。
  * フェーズ5以降: ここを補正モデルの予測＋出走確率に置き換える。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

MS_TO_KN = 1.94384
JST = timezone(timedelta(hours=9))

# ============================================================
# 出走条件(★自分のスポット・技量・セイルに合わせて必ず調整する)
# ============================================================
SAIL_MIN_MS = 4.0           # これ未満は走れない(フォイルが浮かない)  ≈8kt
SAIL_MAX_MS = 12.0          # これ超は強すぎ/危険                    ≈23kt
# 風向条件は一旦無効化している(2026-08-02)。
# 従来の 45〜270°(NE〜S〜W) は「南向きの浜」の暫定値で、牛臥の実際の
# 安全な向きに合わせた検証をしていなかったため、当面は風速のみで判定する。
# 復活させるときは弧を入れ直すだけでよい(空でなければ再び効く)。
# 風向は「風が吹いてくる向き」。オンショア〜サイドが安全、
# オフショア(陸→海)は流される危険があるので除外する向き。
SAFE_DIR_ARCS: list[tuple[float, float]] = []   # 空 = 風向で絞り込まない

# 出走可否は「補正済み加重平均風速」が上のレンジに入るかで決める。
# calibration.json が無いときだけ、全モデルの単純平均風速にフォールバックする。
# モデル別の可否(per_model)と一致数(agree)は参考表示として残すが、判定には使わない。

# 出走判定の対象時刻(JST)。海風が安定する午前・午後のセッションを狙う。
# 取得後 JUDGE_WITHIN_H 時間以内に来る、各時刻ちょうどの予測値で判定する。
TARGET_HOURS_JST = [10, 14]
JUDGE_WITHIN_H = 24


# ============================================================
# 風向ユーティリティ
# ============================================================

def uv_to_speed_dir(u: float, v: float) -> tuple[float, float]:
    speed = math.hypot(u, v)
    dir_deg = math.degrees(math.atan2(-u, -v)) % 360
    return speed, dir_deg


def compass16(deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg + 11.25) % 360 // 22.5)]


def dir_in_arcs(deg: float, arcs=SAFE_DIR_ARCS) -> bool:
    if not arcs:                    # 弧の指定なし = 風向による絞り込みをしない
        return True
    for a, b in arcs:
        if a <= b:
            if a <= deg <= b:
                return True
        else:                       # 0°をまたぐ場合
            if deg >= a or deg <= b:
                return True
    return False


# ============================================================
# DB アクセス
# ============================================================

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_fetched_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(fetched_at) AS fa FROM forecasts").fetchone()
    return row["fa"] if row else None


def snapshot(conn: sqlite3.Connection, fetched_at: str,
             max_lead: float = 48.0) -> list[dict]:
    """最新スナップショットの将来予測を全モデル分返す。"""
    q = """SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg,
                  wind_u, wind_v
           FROM forecasts
           WHERE fetched_at = ? AND lead_hours BETWEEN 0 AND ?
           ORDER BY model, valid_time"""
    return [dict(r) for r in conn.execute(q, (fetched_at, max_lead))]


def nearest_rows(rows: list[dict], target_lead: float) -> dict[str, dict]:
    """各モデルで lead_hours が target に最も近い行を選ぶ。"""
    best: dict[str, dict] = {}
    for r in rows:
        m = r["model"]
        if m not in best or abs(r["lead_hours"] - target_lead) < \
                abs(best[m]["lead_hours"] - target_lead):
            best[m] = r
    return best


# ============================================================
# 補正(calibration.json)
# ============================================================

_CAL_CACHE: dict | None = None
_CAL_LOADED = False


def load_calibration() -> dict | None:
    """calibration.json を読む(プロセス内で1回だけ)。無ければ None。"""
    global _CAL_CACHE, _CAL_LOADED
    if _CAL_LOADED:
        return _CAL_CACHE
    _CAL_LOADED = True
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    try:
        with open(path, encoding="utf-8") as f:
            _CAL_CACHE = json.load(f)
    except Exception:
        _CAL_CACHE = None
    return _CAL_CACHE


def corrected_weighted(chosen: dict[str, dict], vt_jst: datetime | None,
                       cal: dict | None = None) -> tuple[float | None, float | None]:
    """補正済み加重平均の (風速 m/s, 風向 度) を返す。

    * 風速: 時間帯別バイアスを引いてから weight で加重平均する。
    * 風向: 時間帯別バイアス分を u/v ベクトルで逆回転してから dir_weight で加重平均。
      風向に情報を持たないモデルは dir_weight=0 で自動的に外れる。
    calibration.json が無い / 該当モデルが無い場合は None を返す(呼び出し側で単純平均へ)。
    """
    cal = cal if cal is not None else load_calibration()
    if not cal or vt_jst is None:
        return None, None
    models = cal.get("models", {})
    hour = str(vt_jst.hour)
    ws_sum = w_tot = 0.0
    wu = wv = wd_tot = 0.0
    for m, r in chosen.items():
        mc = models.get(m)
        if not mc:
            continue
        wt = mc.get("weight", 0.0)
        spd = r["wind_speed_ms"]
        if spd is not None and wt > 0:
            bias = mc.get("hourly_bias", {}).get(hour, mc.get("bias_overall", 0.0))
            ws_sum += wt * max(0.0, spd - bias)
            w_tot += wt
        dwt = mc.get("dir_weight", wt)
        if r["wind_u"] is not None and dwt > 0:
            b_rad = math.radians(mc.get("hourly_dir_bias", {}).get(
                hour, mc.get("dir_bias", 0.0)))
            u_c = r["wind_u"] * math.cos(-b_rad) - r["wind_v"] * math.sin(-b_rad)
            v_c = r["wind_u"] * math.sin(-b_rad) + r["wind_v"] * math.cos(-b_rad)
            wu += dwt * u_c; wv += dwt * v_c; wd_tot += dwt
    speed = ws_sum / w_tot if w_tot > 0 else None
    if wd_tot > 0 and (wu != 0.0 or wv != 0.0):
        _, dir_deg = uv_to_speed_dir(wu / wd_tot, wv / wd_tot)
    else:
        dir_deg = None
    return speed, dir_deg


# ============================================================
# 出走可否の判定 — 補正済み加重平均風速が出走レンジに入るか
# ============================================================

def _aggregate(chosen: dict[str, dict], fa: str) -> dict | None:
    """モデル別の行(model->row)を集計して判定結果を返す。"""
    if not chosen:
        return None
    per_model = []
    us, vs, speeds = [], [], []
    n_sail = 0
    valid_time = None
    for m, r in sorted(chosen.items()):
        sp_ms = (r["wind_speed_ms"] or 0)
        di = r["wind_dir_deg"]
        # 風向条件は一旦無効(SAFE_DIR_ARCS を参照)。風速レンジのみで見る。
        # これはモデル別の参考表示用で、出走可否そのものは下の加重平均で決める。
        ok = SAIL_MIN_MS <= sp_ms <= SAIL_MAX_MS
        n_sail += int(ok)
        speeds.append(sp_ms)
        if r["wind_u"] is not None:
            us.append(r["wind_u"]); vs.append(r["wind_v"])
        valid_time = r["valid_time"]
        per_model.append({"model": m, "speed_ms": round(sp_ms, 1),
                          "dir_deg": di,
                          "compass": compass16(di) if di is not None else "-",
                          "sailable": ok, "lead": r["lead_hours"]})

    mean_u = sum(us) / len(us) if us else 0.0
    mean_v = sum(vs) / len(vs) if vs else 0.0
    _, raw_dir = uv_to_speed_dir(mean_u, mean_v)
    raw_sp = sum(speeds) / len(speeds) if speeds else 0.0
    n = len(per_model)
    vt_jst = datetime.fromisoformat(valid_time).astimezone(JST) if valid_time else None

    # ── 判定に使う代表値: 補正済み加重平均(取れなければ単純平均) ──
    w_speed, w_dir = corrected_weighted(chosen, vt_jst)
    speed = round(w_speed if w_speed is not None else raw_sp, 1)
    dir_deg = w_dir if w_dir is not None else raw_dir
    source = "corrected_weighted" if w_speed is not None else "simple_mean"

    return {
        "fetched_at": fa,
        "valid_time_jst": vt_jst,
        "mean_speed_ms": speed,          # 判定にも表示にも使う代表風速
        "mean_dir_deg": round(dir_deg),
        "mean_compass": compass16(dir_deg),
        "speed_source": source,
        "raw_mean_speed_ms": round(raw_sp, 1),   # 補正前の単純平均(参考)
        "agree": f"{n_sail}/{n}",                # 参考表示。判定には使わない
        "n_sail": n_sail, "n_models": n,
        "sailable": SAIL_MIN_MS <= speed <= SAIL_MAX_MS,
        "per_model": per_model,
    }


def evaluate_window(conn: sqlite3.Connection, target_lead: float,
                    fetched_at: str | None = None) -> dict | None:
    """target_lead 時間先の出走可否(リード基準)。"""
    fa = fetched_at or latest_fetched_at(conn)
    if not fa:
        return None
    ev = _aggregate(nearest_rows(snapshot(conn, fa), target_lead), fa)
    if ev:
        ev["target_lead"] = target_lead
        ev["label"] = f"+{int(target_lead)}h"
    return ev


def next_clock_valid_time(conn: sqlite3.Connection, fa: str, hour_jst: int,
                          within_h: float = JUDGE_WITHIN_H) -> str | None:
    """fa 以降・within_h 時間以内で、JST が hour_jst:00 ちょうどの valid_time(UTC ISO)を返す。"""
    q = """SELECT DISTINCT valid_time FROM forecasts
           WHERE fetched_at=? AND lead_hours BETWEEN 0 AND ?
           ORDER BY valid_time"""
    for r in conn.execute(q, (fa, within_h)):
        vt = datetime.fromisoformat(r["valid_time"]).astimezone(JST)
        if vt.hour == hour_jst and vt.minute == 0:
            return r["valid_time"]
    return None


def next_n_clock_valid_times(conn: sqlite3.Connection, fa: str,
                              target_hours: list[int], n: int = 2,
                              within_h: float = 48.0) -> list[str]:
    """現在時刻より後で来る target_hours の時刻を、昇順で最大 n 個返す。"""
    now_jst = datetime.now(JST)
    q = """SELECT DISTINCT valid_time FROM forecasts
           WHERE fetched_at=? AND lead_hours BETWEEN 0 AND ?
           ORDER BY valid_time"""
    results = []
    for r in conn.execute(q, (fa, within_h)):
        vt = datetime.fromisoformat(r["valid_time"]).astimezone(JST)
        if vt <= now_jst:
            continue
        if vt.minute == 0 and vt.hour in target_hours:
            results.append(r["valid_time"])
            if len(results) >= n:
                break
    return results


def evaluate_at(conn: sqlite3.Connection, vt_iso: str,
                fetched_at: str | None = None) -> dict | None:
    """指定した valid_time の出走可否を評価する。"""
    fa = fetched_at or latest_fetched_at(conn)
    if not fa:
        return None
    chosen = {r["model"]: dict(r) for r in conn.execute(
        """SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg,
                  wind_u, wind_v
           FROM forecasts WHERE fetched_at=? AND valid_time=? ORDER BY model""",
        (fa, vt_iso))}
    ev = _aggregate(chosen, fa)
    if ev:
        vt_jst = datetime.fromisoformat(vt_iso).astimezone(JST)
        ev["target_hour"] = vt_jst.hour
        ev["label"] = f"{vt_jst.hour:02d}:00"
    return ev


def evaluate_clock(conn: sqlite3.Connection, hour_jst: int,
                   fetched_at: str | None = None,
                   within_h: float = JUDGE_WITHIN_H) -> dict | None:
    """取得後 within_h 時間以内に来る JST hour_jst:00 時点の出走可否を評価する。"""
    fa = fetched_at or latest_fetched_at(conn)
    if not fa:
        return None
    vt_iso = next_clock_valid_time(conn, fa, hour_jst, within_h)
    if not vt_iso:
        return None
    chosen = {r["model"]: dict(r) for r in conn.execute(
        """SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg,
                  wind_u, wind_v
           FROM forecasts WHERE fetched_at=? AND valid_time=? ORDER BY model""",
        (fa, vt_iso))}
    ev = _aggregate(chosen, fa)
    if ev:
        ev["target_hour"] = hour_jst
        ev["label"] = f"{hour_jst:02d}:00"
    return ev
