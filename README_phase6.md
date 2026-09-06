# フェーズ6：可視化・ダッシュボード・スマホ通知

フェーズ1で貯めた `wind.db` を読み、複数モデルの風予測を**可視化**し、
走れそうな時に**スマホへ通知**する。判定ロジックは差し替え可能にしてあり、
フェーズ4〜5の補正モデルができたら中身を入れ替えるだけでよい。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `phase6_common.py` | DB読み込み・アンサンブル平均・出走可否判定(共通) |
| `dashboard.py` | Streamlit ダッシュボード(ブラウザ/スマホで見る) |
| `notify.py` | ntfy / LINE へプッシュ通知(cron で定期実行) |
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
- **LINE**: 特定の人に届けたい場合に使う。設定手順は「3. LINE で特定の人に通知する」。
  かつての **LINE Notify は2025年3月末で終了**したので、後継の
  **LINE Messaging API**(公式アカウント＋チャネル作成が必要)を使う。

---

## 3. LINE で特定の人に通知する

ntfy はアプリを入れた本人だけが受け取る。**特定の人(自分以外の同行者など)へ
LINE で飛ばしたい**場合は、LINE公式アカウントから push する。

> LINE Notify(個人トークンを発行するだけの旧方式)は **2025-03-31 に終了**した。
> ネット上の古い手順はほぼこれなので、そのままでは動かない。

### 3-1. 送る側(公式アカウント)を用意する

1. [LINE Developers](https://developers.line.biz/console/) に
   **「LINEアカウント」でログイン**し、プロバイダーを作る(なければ)。
   メールアドレスでのログインでも作れるが、3-2 の宛先IDの確認で困る。
2. **Messaging API チャネル**を新規作成する。これが送信元の公式アカウントになる。
3. チャネルの「Messaging API設定」で **チャネルアクセストークン(長期)** を発行する。
   これが `LINE_CHANNEL_ACCESS_TOKEN`。**再表示できるが、他人に渡さないこと**
   (持っている人は誰でもこのアカウントとして送信できる)。
4. 同じ画面で **応答メッセージをオフ**にしておくと、こちらが送るだけの静かな
   アカウントになる。

### 3-2. 受け取る側(宛先ID)を調べる

**送りたい相手には、先に公式アカウントを友だち追加してもらう**必要がある。
友だちでない相手には push できない(403 が返る)。

- **自分あて**: LINE Developers の「チャネル基本設定」に出る **Your user ID**
  (`U` で始まる33桁)をそのまま使える。
  ただしこれは **3-1 で LINEアカウントでログインした場合だけ表示される**。
  メールアドレス(ビジネスアカウント)で作ったビジネスIDは LINEアカウントと
  未連携のため、この欄自体が出ない。その場合は後から LINEアカウントを連携するか、
  下のグループ方式を使う。
- **他の人あて**: その人の userId は本人の画面には出ない。webhook を受けて
  `source.userId` を拾う必要がある。手軽なのは次の **グループ方式**。
- **グループ方式(おすすめ)**: LINEでグループを作り、公式アカウントを招待する。
  webhook で受け取る `source.groupId`(`C` で始まる)を宛先にすれば、
  **グループ全員に届く**。人が増減してもこちらの設定は変えなくてよい。

`LINE_TO` には userId / groupId / roomId のどれでも入る。**カンマ区切りで複数指定可**。

### 3-3. 設定して動かす

環境変数(またはコマンドライン引数)で渡す。**未設定なら LINE へは何も送らず、
今までどおり ntfy だけで動く**。

```bash
export LINE_CHANNEL_ACCESS_TOKEN="長期トークン"
export LINE_TO="Uxxxxxxxx...,Cyyyyyyyy..."   # 複数可

python notify.py --test-send  # 判定を通さずテスト通知を1通送る(疎通確認)
python notify.py --dry-run    # 送信せず、宛先ごとに「送る/送信済み」を表示
python notify.py              # ntfy と LINE の両方へ
python notify.py --no-ntfy    # LINE だけへ
```

設定した直後は `--test-send` で確かめる。判定も重複抑止も通さないので、
**出走できそうな日でなくても届くかどうかが分かる**(届かない場合、LINE 側の
原因はたいてい「公式アカウントを友だち追加していない」= 403)。
GitHub Actions からは **notify-test** ワークフローを手動実行すると同じことができる
(予測取得もコミットもしないので、何度回しても本番の生成物に影響しない)。

GitHub Actions から動かす場合は、リポジトリの
**Settings → Secrets and variables → Actions** に
`LINE_CHANNEL_ACCESS_TOKEN` と `LINE_TO` を登録する。`forecast.yml` の通知ステップが
env で拾う。**Secrets を入れるまでワークフローの挙動は一切変わらない。**

### 3-4. 注意点

- **無料枠**: 公式アカウントの無料プランは **push メッセージ月200通**まで
  (2026年時点)。宛先1人=1通と数えるので、宛先3人なら1回の通知で3通消費する。
  超過分は送信されない(429 が返る)。人数が増えるならグループ方式にすると1通で済む。
- **重複抑止は宛先ごと**に持つ(`notify_log` のキーに宛先のハッシュを付ける)。
  ntfy が成功して LINE が失敗した回は、次回 **LINE にだけ**送り直す。
- **userId は wind.db に生のまま保存しない**。このDBは公開リポジトリに push される
  ため、キーには SHA-256 の先頭12桁だけを入れている。
- 恒久的なエラー(401 トークン誤り / 403 友だち未追加 / 400 宛先ID不正)は
  再送しない。429・5xx だけ1回やり直す。

---

## 4. 定期実行(cron 例)

```cron
# 取得(フェーズ1)
10 0,6,12,18 * * * cd /path && python3 collect_forecasts.py >> collect.log 2>&1
# 判定して通知(取得の少し後)
20 0,12 * * *     cd /path && python3 notify.py >> notify.log 2>&1
```

ダッシュボードは上記 systemd で常時稼働させ、開くたびに最新の `wind.db` を読む。

---

## 5. 出走条件の調整(安全のため必読)

`phase6_common.py` 冒頭:

```python
SAIL_MIN_MS = 4.0           # これ未満は走れない  (≈8kt)
SAIL_MAX_MS = 12.0          # これ超は強すぎ/危険 (≈23kt)
SAFE_DIR_ARCS = []          # 空 = 風向で絞り込まない(2026-08-02 に一旦無効化)
MIN_MODELS_AGREE = 2        # 何モデル一致で「可」とするか
TARGET_HOURS_JST = [10, 14] # 判定する時刻(JST)。取得後24時間以内の該当時刻を見る
```

- 風速レンジは**自分の技量とセイルサイズ**に合わせる。
- **風向条件は現在無効**。判定は風速レンジのみで行う。風向はダッシュボード・
  通知に表示はされるが、出走可否には効かない。
- 復活させるときは `SAFE_DIR_ARCS` に弧を入れるだけでよい(空でなければ再び効く)。
  **必ず自分の浜の安全な向き**に直すこと。オンショア(海→陸)やサイドは安全側、
  **オフショア(陸→海)は流される危険**があるため除外する。
  風向は「風が吹いてくる向き」である点に注意。

---

## 6. フェーズ4〜5(補正モデル)との接続

判定は `phase6_common.evaluate_window()` に集約してある。補正モデルができたら:

- `dashboard.py` のチャートに「補正後」系列を1本足す。
- `evaluate_window()` の中を、生予測の合議から **補正後の予測＋出走確率** に置き換える
  (例: 「14:00 に6m/s以上の確率72%」を通知文・サマリに出す)。

呼び出し側(dashboard / notify)は変更不要。表示と判定の窓口を1関数にまとめてあるのは
このためである。
