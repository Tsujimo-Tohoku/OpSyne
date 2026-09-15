# Heroku Cedarのログを接続する

対象：既存のHeroku CedarアプリからHTTPS Log Drainでログを受け取る。GitHub連携は不要。FirやRender等への専用接続、Herokuアカウントの自動連携は含まない。

この機能は設定と受信の基盤であり、質問式の導入画面はまだ提供しない。既存のファイル・一般pushによる接続も引き続き使える。

## 1. サービスとログ接続先を登録

既存の管理画面または管理者Bearer認証付きAPIで、対象サービスとpush sourceを登録する。例：

`POST /api/services`

```json
{"id":"demo-api","name":"Demo API","instance_id":"demo-api-production","owner":"owner"}
```

`POST /api/sources`

```json
{"id":"heroku-demo-api","service_id":"demo-api","name":"Heroku production logs","kind":"push","stale_after_seconds":120,"enabled":true}
```

他のサービス・環境には別のsourceを登録する。受信先にURLを指定するだけでsourceが作られることはない。

## 2. 専用の受信資格情報を設定

所有者が管理する`heroku-drains.json`を用意する。ここには秘密の値を書かず、環境変数名を指定する。

```json
[
  {
    "source_id": "heroku-demo-api",
    "service_id": "demo-api",
    "username": "heroku",
    "password_env": "OPSYNE_HEROKU_DEMO_PASSWORD",
    "drain_token_env": "OPSYNE_HEROKU_DEMO_TOKEN"
  }
]
```

source/service IDはこの設定では英数字・`_`・`.`・`-`、usernameは同じ文字種で64文字まで。パスワードは接続先ごとに生成した32〜256文字の印字可能ASCII（空白を除く）を使用し、管理者・操作担当・承認者のトークンは使わない。ユーザー名が異なっていても同じパスワードを複数sourceへ設定すると起動に失敗する。

`OPSYNE_HEROKU_DRAINS_FILE`へこのファイルの絶対パスを設定し、上記のパスワードとTokenを環境変数／ホスティングのSecret設定へ保存する。空または不正な設定は起動時に拒否される。ファイルの指定がなければ受信APIは404を返す。

Drain作成前などTokenがまだ分からない場合は、`drain_token_env`キーを省略してBasic認証だけで開始できる。Drain作成後に`heroku drains --json -a <app>`等でTokenを確認し、環境変数名を追加して再起動すると、以後Tokenの一致も要求する。指定した環境変数が未設定なら起動しない。資格情報を変えたときも再起動する。

## 3. HTTPSで受信できる場所を用意

CLIの設定例：

```dotenv
OPSYNE_HOST=0.0.0.0
OPSYNE_ALLOWED_HOSTS=opsyne.example.com,localhost,127.0.0.1
OPSYNE_TRUSTED_PROXIES=127.0.0.1
OPSYNE_HEROKU_DRAINS_FILE=/absolute/path/to/heroku-drains.json
```

TLSは前段のプロキシ／ホスティングで終端する。`OPSYNE_TRUSTED_PROXIES`には実際に直前にいるプロキシのIPかネットワークを指定する。空文字列で転送ヘッダーを信頼しない設定にもできる。`*`や`0.0.0.0/0`等の全アドレス信頼は受け付けない。

アプリは生の`X-Forwarded-Proto`を信頼しない。指定したプロキシからUvicornが受け取ったHTTPS情報だけを使用する。プロキシは利用者からの転送ヘッダーを除去・再設定し、アプリの待受ポートへ直接インターネットから到達できないようにする。

外部の入口では`POST /api/drains/heroku/...`だけを公開し、管理UI・既存APIは私的な入口へ制限する。前段でTLS、本文サイズ、時間制限、レート・同時接続制限を設定する。`0.0.0.0`指定だけで公開構成の準備が完了するわけではない。単一プロセス／永続ディスクの構成を維持し、複数ワーカーや一時ディスクへの配置は行わない。

```text
python scripts/dev.py run --locked python -m opsyne serve --env-file .env
```

ポートの優先順位は`--port` → `OPSYNE_PORT` → プロバイダーの`PORT` → `8765`。`--host`は`OPSYNE_HOST`より優先する。既定の待受は`127.0.0.1`のまま。

## 4. Heroku側から転送

対象アプリがCedarであることを確認し、HTTPS Drainの接続先を次のパスに設定する。

```text
https://opsyne.example.com/api/drains/heroku/heroku-demo-api
```

HerokuのHTTPS Drain設定には専用のBasicユーザー／パスワードも渡す。資格情報をURLへ含める必要があるため、値を公開ドキュメント・Issue・シェル履歴・操作ログへ残さない方法で設定する。具体的なCLI手順は[Heroku公式](https://devcenter.heroku.com/articles/log-drains#https-drains)を参照する。

所有者のBearerトークンをHerokuへ渡さない。一般push APIの`/api/sources/{id}/ingest`はJSON用であり、Logplexの送信先には使わない。

## 5. 接続と解析を分けて確認

1. 対象アプリから合成テストログを出す。
2. 認証付き`GET /api/evidence?source_id=heroku-demo-api`で受信とサービスの対応を確認する。
3. `raw_bytes_b64`を復号すると個々のSyslogメッセージの原文バイトを取得できる。`digest`もこのバイト列を対象とする。
4. `payload`には解析用JSONを保存する。`raw`が元のログの表示用文字列、`heroku`が取得できたヘッダー、`message`が本文、`app`が曖昧でないアプリJSONである。
5. アプリJSONの`app.level`、`app.result`等を、通常の解析ルール作成・サンプル検証・別主体承認で結び付ける。
6. 新しいログで検知を確認する。保存済みログへの適用には既存の再解析を使う。

例えばアプリが次をstdoutへ出した場合：

```json
{"level":"ERROR","result":"FAILURE","message":"database connection failed"}
```

ヘッダーを分離できれば、解析用の`app.level`が`ERROR`、`app.result`が`FAILURE`となる。ルールの条件には`transport=heroku_logplex_v1`と`decode_status=json_object`を指定できる。Connector自身はERROR等の意味を確定せず、既存の承認済みルールがseverity/outcomeを決める。

通常のJSON形式自動提案も、AI接続・予算・サンプル・形式対応等の既存条件に従う。自動提案の実LLM品質は今回検証していない。非JSONのメッセージ、項目不足、不明な意味、非UTF-8を正常として扱わない。

## 受信結果と制約

| 結果 | 意味 |
|---|---|
| 204、本文なし、Content-Length: 0 | 全メッセージの原本を保存した。重複した同一原文も成功として返す |
| 400 / 415 / 413 | フレーム・ヘッダー・形式・サイズ等が不正。値を直してから再送する |
| 401 / 403 | 専用資格情報・Token・source/serviceの対応を確認する |
| 404 | 受信が未設定、または資格情報に対応する登録対象が存在しない |
| 409 | 同じsourceとexternal_idに異なる原文がある。IDを使い回さない |
| 5xx / 接続切断 | 一部保存の可能性がある。同じFrame ID・順序・内容で再送する |

本文2,000,000バイト、個々の原文1MiB、投影payloadは既存RawInputの文字数上限、最大10,000メッセージ。アプリJSONの抽出は本文65,536文字まで。上限超過やフレーム不正は保存前に拒否する。500件ずつ保存するため、ストレージ障害では一部だけ保存済みになる場合がある。

204は解析・検知・復旧の完了ではない。既存の観測ワーカーがpendingを処理する。無通信は正常の証拠ではなく、Logplexによる完全配送も保証しない。coverageの最終受信・stale等を確認する。

実Herokuへの接続と公開TLSでの配送は、利用する環境で別途確認する。仕様・信頼境界・原文と投影の判断は[ADR 0006](adr/0006-heroku-log-intake.md)を参照する。
