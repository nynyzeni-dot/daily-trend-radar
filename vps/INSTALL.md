# daily-trend-radar deliver.py — VPS設置手順

★このファイルは手順書のみ。VPSへの実際の書き込みは番人(vps-ops-guardian)チェックを
通してから、別途行うこと。ここではまだ何もVPSに置いていない。

## 1. 配置するファイル

ローカル `C:\dev\projects\daily-trend-radar\vps\` の以下をVPSにコピーする。
★設置先は `/usr/local/bin/`(VPSの既存バッチ3本と同じ流儀)に統一している。

| ローカル | VPS配置先 | 備考 |
|---|---|---|
| `deliver.py` | `/usr/local/bin/daily-trend-radar-deliver.py` | 本体 |
| `config.env.example` | 参考のみ。実ファイルは下記2で新規作成 | コピーしない(値を埋めた別ファイルを作る) |

## 2. 設定ファイルを作る

```
sudo mkdir -p /etc/daily-trend-radar
sudo nano /etc/daily-trend-radar/config.env
```

`config.env.example` の中身をベースに、以下を埋める。

- `LINE_CHANNEL_ACCESS_TOKEN` … LINE Developersのチャネルアクセストークン
- `LINE_USER_ID` … 配信先のLINE User ID(既存Botと同じ宛先なら使い回し可)
- `LINE_PING_URL` … healthchecks.ioのcheck URL(このジョブ専用に新規発行する。
  vps-intrusion-check用と共用しない)
- `REPORT_JSON_URL` / `STATE_FILE` は省略可(デフォルトのままでよければ書かない)

パーミッションを締める(トークンが平文で入るため):

```
sudo chmod 600 /etc/daily-trend-radar/config.env
sudo chown root:root /etc/daily-trend-radar/config.env
```

## 3. 状態ファイル・ログ用のディレクトリ

`deliver.py`が自動で作成するが、権限周りを先に整えておくなら:

```
sudo mkdir -p /var/lib/daily-trend-radar
sudo touch /var/log/daily-trend-radar.log
```

cronを実行するユーザー(通常root)が書き込める場所であること。

## 4. cronに登録

`crontab.txt` の1行を `crontab -e` で追加する(root権限で実行する想定)。

```
35 7 * * * /usr/bin/python3 /usr/local/bin/daily-trend-radar-deliver.py >> /var/log/daily-trend-radar-cron.log 2>&1
```

★7:35なのはVPS侵入チェック(毎朝7:30実行)との丸かぶりを避けるため。

deliver.pyは標準ライブラリのみで書いているのでpipインストール・venvは不要。
`/usr/bin/python3` のバージョンが3.9以上であることだけ確認する(`zoneinfo`を使うため)。

```
python3 --version
```

## 5. 動作確認コマンド

### 手動で1回実行してログを見る

```
python3 /usr/local/bin/daily-trend-radar-deliver.py
echo "exit code: $?"
tail -n 20 /var/log/daily-trend-radar.log
```

- `exit code: 0` かつログに `正常終了` が出ていればOK。
- ネタが無い日(`count=0`)ならLINEは飛ばず、ログに「配信スキップ(ネタなし」と出る。
- 2回連続で実行すると、2回目は「配信スキップ(既に配信済みの日付)」になるはず
  (同日の二重配信防止の確認)。

### 状態ファイルの確認

```
cat /var/lib/daily-trend-radar/last_delivered.txt
```

前回配信(またはスキップ確定)した日付が入っているはず。

### healthchecksのping確認

healthchecks.ioの管理画面で、該当チェックの「Last Ping」が実行直後に更新されているか見る。
異常終了(exit code 非0)のときはpingが打たれない=healthchecks側で「遅延」アラートが鳴る
のが正しい挙動。

### わざと壊して非ゼロ終了を確認したいとき

`REPORT_JSON_URL` を環境変数で一時的に不正なURLに差し替えて実行する(config.envは書き換えない):

```
REPORT_JSON_URL=https://example.invalid/x.json python3 /usr/local/bin/daily-trend-radar-deliver.py
echo "exit code: $?"
```

`exit code: 1` になり、ログに `処理中に異常終了` が出ていればOK。

---

## 6. weekly_youtube.py の設置(毎週土曜9:00 JSTにYouTubeの伸び筋をLINE配信)

★このファイルも手順書のみ。VPSへの実際の書き込みは番人(vps-ops-guardian)チェックを
通してから、別途行うこと。ここではまだ何もVPSに置いていない。

### 6-1. 配置するファイル

| ローカル | VPS配置先 | 備考 |
|---|---|---|
| `weekly_youtube.py` | `/usr/local/bin/daily-trend-radar-youtube.py` | 本体。deliver.pyとは独立して動く(import共有なし) |

### 6-2. 設定ファイル(deliver.pyと共用)

`/etc/daily-trend-radar/config.env` は deliver.py と同じファイルを使う。
`config.env.example` に追記した以下を埋める。

- `YOUTUBE_API_KEY` … Google Cloud ConsoleでYouTube Data API v3を有効化して発行したAPIキー
- `YOUTUBE_STATE_FILE` は省略可(デフォルトのままでよければ書かない)

`LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` はdeliver.pyの設定を使い回す(新規に埋める必要なし)。

★このスクリプトにはhealthchecks.ioのpingは無い(週1実行のため専用の死活監視は未整備)。
その代わり異常終了時は必ず非ゼロで終了し、ログに理由(クォータ超過/ネットワークエラー等)を残す。

### 6-3. cronに登録

`crontab.txt` に追記した以下の1行を `crontab -e` で追加する(deliver.pyの既存行は消さない)。

```
0 9 * * 6 /usr/bin/python3 /usr/local/bin/daily-trend-radar-youtube.py >> /var/log/daily-trend-radar-youtube-cron.log 2>&1
```

weekly_youtube.pyも標準ライブラリのみで書いているのでpipインストール・venvは不要。

### 6-4. 動作確認コマンド

```
python3 /usr/local/bin/daily-trend-radar-youtube.py
echo "exit code: $?"
tail -n 30 /var/log/daily-trend-radar-youtube.log
```

- `exit code: 0` かつログに `正常終了` が出ていればOK。
- 該当動画が無い週はLINEを送らず、ログに「配信対象なし」と出る。
- 2回連続で実行すると、2回目は「実行スキップ(本日は実行済み)」になるはず
  (同日の二重実行防止の確認)。API呼び出しも2回目は発生しない。

### 6-5. クォータ超過の見分け方

`search.list` は1日100回の専用クォータ、`videos.list` は別プール(1日10,000ユニット)。
週1回・キーワード5個の運用なら通常は枯渇しないが、超過時はHTTP 403でログに
`YouTube APIクォータ超過` と明記され、非ゼロ終了する(ネットワークエラーとは別の文言で
区別できる)。この文言が出ていたら、リトライせずクォータの回復(翌日UTC 0時リセット)を待つ。
