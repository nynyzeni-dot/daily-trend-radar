# daily-trend-radar deliver.py — VPS設置手順

★このファイルは手順書のみ。VPSへの実際の書き込みは番人(vps-ops-guardian)チェックを
通してから、別途行うこと。ここではまだ何もVPSに置いていない。

## 1. 配置するファイル

ローカル `C:\dev\projects\daily-trend-radar\vps\` の以下をVPSにコピーする。

| ローカル | VPS配置先 | 備考 |
|---|---|---|
| `deliver.py` | `/var/www/apps/daily-trend-radar/deliver.py` | 本体 |
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
30 7 * * * cd /var/www/apps/daily-trend-radar && /usr/bin/python3 deliver.py >> /var/log/daily-trend-radar-cron.log 2>&1
```

deliver.pyは標準ライブラリのみで書いているのでpipインストール・venvは不要。
`/usr/bin/python3` のバージョンが3.9以上であることだけ確認する(`zoneinfo`を使うため)。

```
python3 --version
```

## 5. 動作確認コマンド

### 手動で1回実行してログを見る

```
cd /var/www/apps/daily-trend-radar
python3 deliver.py
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
REPORT_JSON_URL=https://example.invalid/x.json python3 deliver.py
echo "exit code: $?"
```

`exit code: 1` になり、ログに `処理中に異常終了` が出ていればOK。
