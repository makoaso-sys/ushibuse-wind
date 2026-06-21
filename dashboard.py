#!/usr/bin/env python3
"""
フェーズ6 ダッシュボード — 牛臥海岸の複数モデル風予測をブラウザ/スマホで見る。

起動:
  pip install streamlit pandas
  streamlit run dashboard.py
  # 同じWi-Fiのスマホからは  http://<PCのIP>:8501  で開ける
  # 外から見たいときは下の「公開」をREADME参照(Streamlit Community Cloud 等)

このダッシュボードは現在フェーズ1の生予測を表示する。フェーズ4〜5の補正モデルが
できたら、表示系列に「補正後」を1本足し、判定に出走確率を併記すればよい。
"""

import sqlite3
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

import phase6_common as pc

DB_PATH = "wind.db"
MAX_LEAD = 48

st.set_page_config(page_title="牛臥海岸 風予測", page_icon="🏄", layout="centered")
st.title("🏄 牛臥海岸 風予測")

# --- 更新ボタン ---
if st.button("🔄 最新に更新"):
    st.rerun()

conn = pc.connect(DB_PATH)
fa = pc.latest_fetched_at(conn)
if not fa:
    st.warning("予測データがありません。先に `python collect_forecasts.py` を実行してください。")
    st.stop()

fa_jst = datetime.fromisoformat(fa).astimezone(pc.JST)
st.caption(f"最新取得(発表): {fa_jst:%Y-%m-%d %H:%M} JST　/　"
           f"出走条件 {pc.SAIL_MIN_MS:.0f}〜{pc.SAIL_MAX_MS:.0f}m/s")

# ============================================================
# 出走可否サマリ(12h / 24h)
# ============================================================
cols = st.columns(len(pc.TARGET_HOURS_JST))
for col, hour in zip(cols, pc.TARGET_HOURS_JST):
    ev = pc.evaluate_clock(conn, hour, fetched_at=fa)
    with col:
        if not ev:
            st.metric(f"{hour:02d}:00", "—")
            continue
        vt = ev["valid_time_jst"]
        verdict = "✅ 出走可" if ev["sailable"] else "⚠️ 見送り"
        st.metric(
            label=f"{vt:%m/%d %H:%M} JST" if vt else f"{hour:02d}:00",
            value=f"{ev['mean_speed_ms']:.1f} m/s",
            delta=f"{ev['mean_compass']} ({ev['mean_dir_deg']}°)",
            delta_color="off",
        )
        st.write(f"**{verdict}**　合議 {ev['agree']}")

# ============================================================
# 複数モデルの風速予測チャート(次48時間)
# ============================================================
rows = pc.snapshot(conn, fa, max_lead=MAX_LEAD)
df = pd.DataFrame(rows)
if df.empty:
    st.stop()
df["valid_time"] = pd.to_datetime(df["valid_time"]).dt.tz_convert(pc.JST)
df["speed_ms"] = df["wind_speed_ms"]

band = (alt.Chart(pd.DataFrame({"y": [pc.SAIL_MIN_MS], "y2": [pc.SAIL_MAX_MS]}))
        .mark_rect(opacity=0.08, color="#2f9e44")
        .encode(y="y:Q", y2="y2:Q"))
rules = (alt.Chart(pd.DataFrame({"y": [pc.SAIL_MIN_MS, pc.SAIL_MAX_MS]}))
         .mark_rule(strokeDash=[4, 4], color="#888", opacity=0.7)
         .encode(y="y:Q"))
lines = (alt.Chart(df).mark_line(point=True)
         .encode(
             x=alt.X("valid_time:T", title="予測対象時刻 (JST)"),
             y=alt.Y("speed_ms:Q", title="風速 (m/s)"),
             color=alt.Color("model:N", title="モデル"),
             tooltip=["model", alt.Tooltip("valid_time:T", format="%m/%d %H:%M"),
                      alt.Tooltip("speed_ms:Q", format=".1f"), "wind_dir_deg"],
         ))
st.subheader("風速予測(複数モデル)")
st.altair_chart((band + rules + lines).properties(height=320), use_container_width=True)
st.caption("帯=出走レンジ。複数モデルが帯内で揃えば確度が高い。jma_msm(5km)が局地の本命。")

# ============================================================
# 12h / 24h のモデル別内訳
# ============================================================
st.subheader("モデル別の内訳")
for hour in pc.TARGET_HOURS_JST:
    ev = pc.evaluate_clock(conn, hour, fetched_at=fa)
    if not ev:
        continue
    vt = ev["valid_time_jst"]
    st.markdown(f"**{vt:%m/%d %H:%M} JST**" if vt else f"**{hour:02d}:00**")
    tbl = pd.DataFrame(ev["per_model"])[
        ["model", "speed_ms", "compass", "dir_deg", "sailable"]]
    tbl.columns = ["モデル", "風速m/s", "風向", "度", "出走可"]
    st.dataframe(tbl, hide_index=True, use_container_width=True)

st.caption("※ 現在はフェーズ1の生予測。フェーズ5の補正モデル完成後、"
           "ここに補正後の値と出走確率が加わります。")
