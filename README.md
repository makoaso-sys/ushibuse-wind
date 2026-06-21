# 牛臥海岸 ウィングフォイル風予測 — 最新ファイル一式

静岡県沼津市・牛臥海岸でのウィングフォイルのため、12〜24時間先の風速・風向を
複数の気象モデルから取得し、可視化・通知するシステム。これまでの検討の最新版をまとめたもの。

## まず読むもの

| ファイル | 内容 |
|---|---|
| `documents/wind_forecast_strategy.docx` | 全体戦略書(フェーズ0〜7) |
| `documents/model_comparison.docx` | 取得4モデル(jma_msm等)の比較 |

## コード(フェーズ1: 取得 / フェーズ6: 可視化・通知)

| ファイル | 役割 | 詳細README |
|---|---|---|
| `collect_forecasts.py` | 気象モデル取得 → `wind.db`(SQLite) | `README_phase1.md` |
| `phase6_common.py` | 出走判定の共通ロジック(★しきい値はここで調整) | `README_phase6.md` |
| `dashboard.py` | Streamlit ダッシュボード(自分のPC/サーバで動かす版) | `README_phase6.md` |
| `notify.py` | ntfy でスマホへプッシュ通知 | `README_phase6.md` |
| `generate_dashboard.py` | サーバ不要の静的 HTML+PNG ダッシュボード生成 | `README_serverless.md` |
| `sample_viz.py` | 可視化サンプル単体(`examples/sample_dashboard.png` を生成) | — |

## サーバーレス公開(GitHub Actions + Pages)

`.github/workflows/forecast.yml` を使うと、サーバを立てず、PCがスリープしていても
クラウド側で定期的に取得・生成・公開できる。手順は `README_serverless.md` を参照。
`docs/` には生成例(デモデータ)が入っている。

## 出走判定の仕様(最新)

取得後24時間以内に来る **10:00 と 14:00(JST)** の予測値を、複数モデルの合議で判定する。
出走レンジは **4〜12 m/s**。`phase6_common.py` 冒頭の `TARGET_HOURS_JST` /
`SAIL_MIN_MS` / `SAIL_MAX_MS` / `SAFE_DIR_ARCS` で調整できる
(★ `SAFE_DIR_ARCS` は必ず自分の浜の安全な風向に直すこと)。

## クイックスタート

```bash
pip install -r requirements.txt

# 1) ネット不要でまず動作確認
python collect_forecasts.py --demo

# 2) 実際に牛臥海岸の予測を取得
python collect_forecasts.py

# 3a) ローカルでダッシュボード(ブラウザ)
streamlit run dashboard.py

# 3b) または静的ページを生成(docs/index.html, docs/forecast.png)
python generate_dashboard.py

# 4) スマホへ通知(ntfyのトピック名を指定)
python notify.py --topic <自分のntfyトピック名>
```

## 現在地と次の一手

現在はフェーズ1(取得)とフェーズ6(可視化・通知)が動く状態で、判定は複数モデルの
**生予測の合議**(補正前)。次はフェーズ0(センサー設置・記録)とフェーズ2〜5
(現地データによる局地補正モデルの構築)に進むと、判定の精度と確からしさが上がる。
詳細は `documents/wind_forecast_strategy.docx` を参照。
