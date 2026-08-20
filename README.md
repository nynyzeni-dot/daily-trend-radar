# daily-trend-radar
デイリートレンド調査レポート - 毎日AI関連の動画演出ノウハウ・AIツールニュースを自動収集して公開

## これは何か
「AIニュース週報」（ai-news-weekly）の日次版。クラウドのroutineが毎日 `PROMPT.md` を読んで実行し、以下を作る：

- `days/YYYY-MM-DD.html` … その日のレポート（保存版）
- `index.html` … 最新日の内容
- `latest.json` … LINE通知スクリプト（別リポジトリ管理）が読む要約データ

対象ジャンルは2つだけ：**A. 動画の作り方・演出の新手法**（3〜5本）と **B. AIツールのニュース**（0〜3本、週報と重複させない）。

## どう動くか
1. クラウドのroutineが `PROMPT.md` を上から実行
2. `days/` の直近5日分と、同じサンドボックスにクローンされた `ai-news-weekly` の `weeks/` 直近2回分を読んで重複を避ける
3. WebSearch → WebFetchで出典を実地確認 → HTML化 → `latest.json` 書き出し
4. commit & push
5. 死活監視へping

ネタが1件も無かった日は `days/` にHTMLを作らず、`latest.json` に `{"count":0}` だけ書いて「今日はなし」を記録する。

## 手順書を直すときはどこを触るか
実行ロジックはすべて `PROMPT.md` にある。日々の挙動を変えたいとき（対象ジャンル・件数・重複チェックの範囲など）はここだけ編集すればよい。

- デザイン（配色・カード構造）を変えたいとき → `index.html` のCSSを直し、その方針を `PROMPT.md` の「5. HTMLを書く」に書き足す
- LINE通知の中身を変えたいとき → このリポジトリではなく、`latest.json` を読む側のVPSスクリプトを直す（このリポジトリの管轄外）
- 死活監視のURL → `PROMPT.md` 手順9の `<HEALTHCHECK_PING_URL_ROUTINE>` を実際のURLに差し替える

## ブランドカラー
メイン `#D4722A` ／サブ `#E8944A` ／アクセント `#F5C28A` ／ダーク基調（`C:\dev\agents\shared\brand_colors.md` が正本）
