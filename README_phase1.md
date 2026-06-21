# フェーズ1：気象モデル取得パイプライン

牛臥海岸(静岡県沼津市)の座標に対して、複数の気象モデルの **12〜24時間先予測**を
Open-Meteo から定期取得し、SQLite に「縦持ち(1予測点 = 1行)」で蓄積する。
フェーズ2(学習テーブルの結合)の入力になる。

## 構成

| ファイル | 内容 |
|---|---|
| `collect_forecasts.py` | 取得・u/v分解・SQLite保存の本体(設定は冒頭の定数) |
| `requirements.txt` | 依存(`requests` のみ) |
| `wind.db` | 実行すると生成される SQLite(初回自動作成) |

## セットアップ

```bash
pip install -r requirements.txt
```

## 使い方

```bash
# ネット不要。合成データで一連の流れを検証(まずこれで動作確認)
python collect_forecasts.py --demo

# 実際に Open-Meteo から取得して保存
python collect_forecasts.py

# 保存先を指定
python collect_forecasts.py --db /data/wind.db
```

実行するたびに、その時刻(`fetched_at`)のスナップショットとして将来予測が追記される。
1日2回ほど回し続けることで、(予報, 実測)ペアの「予報」側が貯まっていく。

## 定期実行(cron 例)

12時間ごと(00:10 / 12:10 UTC)に取得する例:

```cron
10 0,12 * * * cd /path/to/project && /usr/bin/python3 collect_forecasts.py --db /data/wind.db >> /data/collect.log 2>&1
```

Open-Meteo の無料枠は1日約1万リクエストなので、数モデル×1日数回はまったく問題ない。

## 保存スキーマ(`forecasts` テーブル)

| 列 | 説明 |
|---|---|
| `model` | モデル識別子(例 `jma_msm`) |
| `fetched_at` | **取得時刻 UTC**。発表時刻の代理。リーク防止の基準時刻 |
| `valid_time` | 予測対象時刻 UTC |
| `lead_hours` | `valid_time - fetched_at`(時間) |
| `wind_speed_ms` / `wind_dir_deg` | 風速(m/s)・風向(度) |
| `wind_u` / `wind_v` | 東向き・北向き成分(m/s)。取得時に分解済み |
| `surface_pressure_hpa` / `temperature_2m_c` | 気圧・気温 |
| `latitude` / `longitude` | 取得座標 |

`UNIQUE(model, fetched_at, valid_time)` で同一バッチの重複投入を防ぐ。

## 中身の確認

```bash
sqlite3 wind.db "SELECT model, COUNT(*) FROM forecasts GROUP BY model;"

# 直近スナップショットの 12h 近傍予測
sqlite3 wind.db "
SELECT model, valid_time, lead_hours, wind_speed_ms, wind_dir_deg
FROM forecasts
WHERE lead_hours BETWEEN 11 AND 13
ORDER BY fetched_at DESC, model LIMIT 20;"
```

## 設計上のポイント

- **`fetched_at` を発表時刻の代理にする。** Open-Meteo の通常 forecast API はモデルの
  初期時刻(init)を返さない。そこで「取得時点で実際に手に入っていた情報」= `fetched_at`
  を基準にし、`lead_hours = valid_time − fetched_at` とする。これは保守的で、過去の情報を
  先取りしない(フェーズ2のリーク防止に直結)。
- **1モデル = 1リクエスト。** 応答キーが素直(suffix なし)になり、あるモデルの失敗を
  そのモデルだけに閉じ込められる。空応答のモデルはスキップしてログに残す。
- **単位は m/s。** u/v 計算が素直。表示で kt が要るなら ×1.94384(1kt = 0.514 m/s)。
- **時刻はすべて UTC。** 表示のときだけ JST(UTC+9)に変換する。

## モデルの増減

`collect_forecasts.py` 冒頭の `MODELS` を編集する。AI 系(`ecmwf_aifs025`,
`gfs_graphcast025` など)は識別子が変わることがあるため任意扱いにしてある。
無効な ID があっても、その行が「データなし」でスキップされるだけで全体は止まらない。
利用可能なモデル識別子は Open-Meteo のドキュメントで確認できる。

## 次の一手(フェーズ2)

この `forecasts` テーブルと、フェーズ0で貯めるセンサー実測を、
`valid_time` をキーに結合して (特徴量 X, 正解 y) の学習テーブルを作る。
`fetched_at` / `lead_hours` を使い「発表時点で使えた情報だけ」で特徴量を組むのが要点。
