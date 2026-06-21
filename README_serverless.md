# サーバーレス・ダッシュボード（GitHub Actions + Pages）

サーバを立てず、PCがスリープでも更新され、スマホからどこでも見られる方式。

## しくみ

```
GitHub Actions(クラウドで定期実行)
   ├─ collect_forecasts.py   … Open-Meteo から最新予測を取得 → wind.db
   ├─ generate_dashboard.py  … docs/index.html + docs/forecast.png を生成
   └─ git push               … 生成物をリポジトリに反映
          ↓
GitHub Pages(静的ホスティング) … docs/ を公開
          ↓
スマホのブラウザ … https://<ユーザー名>.github.io/<リポジトリ>/ を開くだけ
```

取得も生成もすべて **GitHub のサーバ上**で動くので、あなたのPCの電源状態に依存しない。
画像(PNG)とHTMLを「置くだけ」の方式そのもの。

## リポジトリに置くファイル

```
collect_forecasts.py        # フェーズ1の取得スクリプト
phase6_common.py            # 共通ロジック(しきい値・判定)
generate_dashboard.py       # 静的ダッシュボード生成
requirements.txt            # requests, matplotlib, numpy
.github/workflows/forecast.yml   # 定期実行ワークフロー
# docs/ は自動生成される(最初の実行後にできる)
```

## セットアップ手順

1. **GitHubリポジトリを作る**。
   無料の GitHub Pages は**公開(public)リポジトリ**が必要(風データは機微でないので問題なし)。
   非公開にしたい場合は GitHub Pro、または後述の Cloudflare Pages を使う。

2. 上記ファイルを push する。`wind.db` を `.gitignore` に入れない(履歴を貯めるため)。

3. **ワークフローを1回手動実行**する。
   リポジトリの **Actions** タブ → "forecast-dashboard" → **Run workflow**。
   これで `docs/index.html` と `docs/forecast.png` が生成・push される。

4. **Pages を有効化**する。
   **Settings → Pages → Build and deployment → Source: Deploy from a branch →
   Branch: `main` / フォルダ: `/docs`** を選んで Save。

5. 1分ほど待ち、`https://<ユーザー名>.github.io/<リポジトリ>/` をスマホで開く。
   ブラウザの「ホーム画面に追加」でアプリのように使える。

以降は cron(既定3時間ごと)で自動更新される。

## 知っておくべき注意点

- **スケジュールはUTC**。`cron: "0 */3 * * *"` は3時間ごと。混雑時は数分〜十数分遅れることがある。
- **60日間リポジトリに動きがないと**スケジュールが自動停止する。手動実行か commit で復活する。
- **更新の反映**: 画像は `forecast.png?v=...` でキャッシュ回避。Pages のCDNは約10分キャッシュ、
  ページは30分ごとに自動再読込。数時間ごとの更新には十分。
- **`wind.db` を毎回コミット**するためリポジトリは少しずつ増える。フェーズ2の学習用に
  履歴を残す設計。履歴が不要なら、ワークフローの `git add` から `wind.db` を外せばよい
  (表示は毎回最新を取得するので問題ない)。古いスナップショットは定期的に間引いてもよい。

## スマホ通知も同時に出す(任意)

ワークフローの「通知」ステップのコメントを外し、リポジトリの
**Settings → Secrets and variables → Actions** に `NTFY_TOPIC`(自分のntfyトピック名)を登録すると、
クラウド側から走れそうな時にスマホへプッシュ通知も飛ぶ(これもPC非依存)。

## 非公開にしたい場合の代替

- **Cloudflare Pages / Netlify**: 同様に GitHub Actions で `docs/` を生成し、そちらにデプロイ。
  アクセス制限(Cloudflare Access 等)で自分だけに限定できる。
- **GitHub Pro**: 非公開リポジトリでも Pages を使える。

## ローカルでの確認

```bash
pip install -r requirements.txt
python collect_forecasts.py        # または --demo
python generate_dashboard.py       # docs/ に生成
# docs/index.html をブラウザで開けば見た目を確認できる
```
