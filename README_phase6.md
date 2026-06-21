# フェーズ6：可視化・ダッシュボード・スマホ通知

フェーズ1で貯めた `wind.db` を読み、複数モデルの風予測を**可視化**し、
走れそうな時に**スマホへ通知**する。判定ロジックは差し替え可能にしてあり、
フェーズ4〜5の補正モデルができたら中身を入れ替えるだけでよい。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `phase6_common.py` | DB読み込み・アンサンブル平均・出走可否判定(共通) |
| `dashboard.py` | Streamlit ダッシュボード(ブラウザ/スマホで見る) |
| `notify.py` | ntfy でスマホへプッシュ通知(cron で定期実行) |
| `sample_dashboard.png` | 可視化のイメージ(合成データ) |

## セットアップ

```bash
pip install streamlit pandas altair requests
```

`phase6_common.py` 冒頭の **出走条件は必ず自分用に調整**すること(後述)。

---

## 1. 可視化・ダッシュボード

```bash
streamlit run dashboard.py
```

ブラウザで `http://localhost:8501` が開く。表示内容:

- 10:00 / 14:00(JST)の出走可否サマリ(平均風速・風向・モデル合議)
- 次48時間の複数モデル風速チャート(出走レンジの帯つき。`jma_msm` が局地の本命)
- 10:00 / 14:00 のモデル別内訳テーブル

### スマホから見る

- **同じWi-Fi内**: PCのIPを調べて `http://<PCのIP>:8501` をスマホで開く
  (`streamlit run dashboard.py --server.address 0.0.0.0` で起動)。
- **どこからでも見たい**: 次のいずれか。
  - **Streamlit Community Cloud**(無料): GitHubに上げて公開。ただし `wind.db` も
    一緒に更新する必要があるので、DBをクラウド(例: S3)に置くか、収集も
    クラウド側で回す構成にする。個人利用なら下のVPS/ラズパイが素直。
  - **小さなVPS or 自宅のラズパイ**で収集とダッシュボードを常時稼働させ、
    **Tailscale**等のVPNで自分のスマホからだけ見えるようにする(公開しないので安全)。
  - **サーバを立てたくない**場合は、cronで定期的に画像(PNG)やHTMLを生成し、
    それだけを置く方式でもよい(`sample_dashboard.png` を作った要領)。

### 常時稼働(例: systemd)

```ini
# /etc/systemd/system/wind-dashboard.service
[Service]
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
Restart=always
```

---

## 2. スマホ通知(ntfy)

**ntfy** は無料・アカウント不要・オープンソースで、個人通知に最適。

1. スマホに **ntfy** アプリを入れる(iOS / Android)。
2. 推測されにくいトピック名を決めて購読する(例: `ushibuse-wind-9f3k2x`)。
3. `notify.py` の `NTFY_TOPIC` を同じ名前にする(または `--topic` で渡す)。

```bash
python notify.py --dry-run     # 送信せず内容だけ確認
python notify.py               # 実際に送信
```

走れそうな時に、こんな通知が届く:

```
🏄 出走チャンス 14:00
06/21 14:00 JST 予測
風速 8.5m/s / 風向 SSW(200°)
モデル合議 4/4 が出走可
(生予測の合議・補正前)
```

同じ予測対象日×リードには1回だけ送るよう重複抑止が入っている(`notify_log` テーブル)。
`NOTIFY_ONLY_WHEN_SAILABLE = False` にすれば、毎回その時の状況を送る。

### 通知先の選択肢

- **ntfy**(既定): 最も手軽。上記のとおり。
- **Telegram**: ボットを作り、`https://api.telegram.org/bot<TOKEN>/sendMessage` に
  `chat_id` と `text` をPOSTするだけ。無料で安定。
- **LINE**: かつての **LINE Notify は2025年3月末で終了**したため使えない。
  LINEで受けたい場合は後継の **LINE Messaging API**(公式アカウント＋チャネル作成が必要、
  無料枠あり)になる。手軽さでは ntfy / Telegram が上。

---

## 3. 定期実行(cron 例)

```cron
# 取得(フェーズ1)
10 0,6,12,18 * * * cd /path && python3 collect_forecasts.py >> collect.log 2>&1
# 判定して通知(取得の少し後)
20 0,12 * * *     cd /path && python3 notify.py >> notify.log 2>&1
```

ダッシュボードは上記 systemd で常時稼働させ、開くたびに最新の `wind.db` を読む。

---

## 4. 出走条件の調整(安全のため必読)

`phase6_common.py` 冒頭:

```python
SAIL_MIN_MS = 4.0           # これ未満は走れない  (≈8kt)
SAIL_MAX_MS = 12.0          # これ超は強すぎ/危険 (≈23kt)
SAFE_DIR_ARCS = [(100, 260)]  # 安全な風向(吹いてくる向き)。★自分の浜に合わせる
MIN_MODELS_AGREE = 2        # 何モデル一致で「可」とするか
TARGET_HOURS_JST = [10, 14] # 判定する時刻(JST)。取得後24時間以内の該当時刻を見る
```

- 風速レンジは**自分の技量とセイルサイズ**に合わせる。
- `SAFE_DIR_ARCS` は**必ず自分の浜の安全な向き**に直す。オンショア(海→陸)や
  サイドは安全側、**オフショア(陸→海)は流される危険**があるため除外する。
  風向は「風が吹いてくる向き」である点に注意。

---

## 5. フェーズ4〜5(補正モデル)との接続

判定は `phase6_common.evaluate_window()` に集約してある。補正モデルができたら:

- `dashboard.py` のチャートに「補正後」系列を1本足す。
- `evaluate_window()` の中を、生予測の合議から **補正後の予測＋出走確率** に置き換える
  (例: 「14:00 に6m/s以上の確率72%」を通知文・サマリに出す)。

呼び出し側(dashboard / notify)は変更不要。表示と判定の窓口を1関数にまとめてあるのは
このためである。
