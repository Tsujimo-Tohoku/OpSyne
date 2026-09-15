# 利用手順: ローカル v0.1

OpSyneは単一ホスト上で、観測・案件・提案・承認・限定操作・独立確認を扱います。最初は合成デモを使い、実サービスは観測源・操作能力・確認条件を登録して接続します。

## 1. 起動とログイン

リポジトリのルートで実行します。

```text
python scripts/bootstrap.py
python scripts/dev.py run --locked python -m opsyne serve
```

[http://127.0.0.1:8765](http://127.0.0.1:8765) を開きます。状態は `.local/opsyne/`、ログイン用トークンは `.local/opsyne/tokens.json` に初回生成されます。別ディレクトリやポートは `serve --data-dir .local/work --port 8766` で指定できます。同じ状態ディレクトリの同時起動は拒否されます。

| トークンの主体 | 役割 | 主な権限 |
|---|---|---|
| `owner` | admin | 対象・観測源・操作能力・確認条件の登録、提案、操作、復元確認 |
| `operator` | operator | 取込、調査要求、提案、実行、結果確認 |
| `reviewer` | approver | 固定内容の承認・却下、変換定義の承認・失効 |
| `viewer` | viewer | 登録情報・案件・証拠・監査の閲覧 |

`owner` も自分が提案した計画・変換を承認できません。画面右上の接続情報から別主体へ切り替えます。ブラウザの同じタブ内ではトークンをsessionStorageに保持します。

この構成は所有者が全トークンとファイルを管理する前提です。各トークンを別の人へ安全に配布する認証基盤や、組織単位のデータ分離は含みません。証拠閲覧APIには原本が含まれ、LLM送信用のマスクとは別です。

## 2. APIキーなしでデモを一周する

1. `owner` で接続し、概要画面の「合成データで動作を確認」またはサービス画面の「デモを準備」を実行します。
2. 「変換定義」で `demo-json-v1` を開きます。保持済み標本でのプレビューと対象・項目・値・適用条件を確認し、`reviewer` に切り替えて承認します。`owner` に戻り「原本を再解析」で解釈を更新します。
3. 「案件」で **サンプル: 注文処理を復旧してください** を開き、`demo-restore` を選んで理由を入力し、計画を作成します。
4. `reviewer` で計画を開き、根拠、影響、固定された操作、成功・中止条件、対象版、digestを確認して承認します。
5. `owner` または `operator` で承認済み計画を実行します。実行直前にも現在条件を照合します。
6. 実行記録から独立確認を実行します。操作が `SUCCEEDED`、確認が `PASS` になった業務障害案件を解決済みにします。

デモはローカルの合成対象を変更します。実サービスへ通信しません。既存のデモを再準備しても、復旧した対象を故障状態へ戻しません。最初から試す場合は新しい `--data-dir` を使います。LLMキー未設定で調査を要求すると、不足を記録した失敗taskになり、正常扱いにはしません。

## 3. OpenAI APIを設定する

[`.env.example`](../.env.example) を `.env` としてコピーし、必要な値をローカルで設定します。

```text
python scripts/dev.py run --locked python -m opsyne serve --env-file .env
```

| 設定 | 既定・意味 |
|---|---|
| `OPENAI_API_KEY` | 未設定。設定すると実際のOpenAI APIに証拠を送信し、API利用料金が発生 |
| `OPSYNE_DAILY_LLM_CALLS` | `20`。UTC日付ごとのtask開始上限。失敗した試行も消費。範囲1–1000 |
| `OPSYNE_AUTO_INVESTIGATE` | `false`。`true`かつキー設定時は、新規検知案件の調査を自動で予約 |
| 任意の `auth_env` 名 | 登録HTTP接続先のBearer資格情報。確認用と操作用を別名・別権限にする |

`.env` は自動で読み込みません。`--env-file` はUTF-8（BOM可）の `KEY=VALUE` を読み、既存のプロセス環境変数を優先します。値全体を単一・二重引用符で囲めます。変数展開・コマンド実行・複数行値・`export` には対応しません。コメントは独立した `#` 行に書き、変更後はサーバーを再起動します。

モデルは [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) に固定しています。Responses APIへ `store=False`、最大2500出力トークン、30秒タイムアウト、リトライなしで要求します。実装内で別モデルへ自動切替しません。実アカウントの利用可否は実際の接続時に確認が必要です。

案件の「調査」は事実・仮説・不明点・根拠を返します。「変換案を依頼」または `POST /api/cases/{id}/adapter-proposal` はAdapter Agentのtaskを予約します。提案された定義は `DRAFT` で保存され、別主体の承認まで利用されません。意味を確定できなければ `UNKNOWN` や提案保留として残します。

LLMへ渡す証拠は案件内の最大20件・既定12000文字に制限し、一般的な秘密キー／認証文字列をマスクします。マスクは完全な機密分類ではありません。全原本の送信や、LLMへの対象操作Tool・DB接続・署名鍵の公開は行いません。

## 4. APIで実サービスを登録する

以下はPowerShellの例です。接続先URL・ファイルパス・識別子を自分の対象に置き換えます。サーバーを起動したまま、別の端末で実行します。APIの詳細な型は認証後の `GET /api/openapi.json` で取得できます。

```powershell
$base = 'http://127.0.0.1:8765'
$tokens = Get-Content '.local/opsyne/tokens.json' -Raw | ConvertFrom-Json
$ownerHeaders = @{ Authorization = 'Bearer ' + ($tokens | Where-Object actor -eq 'owner').token }
$reviewHeaders = @{ Authorization = 'Bearer ' + ($tokens | Where-Object actor -eq 'reviewer').token }
function Invoke-OpSyne {
    param([string]$ApiPath, [hashtable]$Payload, [hashtable]$Headers = $ownerHeaders)
    $json = $Payload | ConvertTo-Json -Depth 15
    Invoke-RestMethod -Method Post -Uri ($base + $ApiPath) -Headers $Headers `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

Invoke-OpSyne '/api/services' @{
    id = 'orders'; name = 'Orders API'; instance_id = 'orders-instance-1'
    version = 1; owner = 'owner'; criticality = 'high'; enabled = $true
}
```

### Push取込

```powershell
Invoke-OpSyne '/api/sources' @{
    id = 'orders-push'; service_id = 'orders'; name = 'Orders application log'
    kind = 'push'; stale_after_seconds = 300
}
$log = @{ format = 'orders-v1'; result = 'error'; level = 'error'; message = 'request failed' }
Invoke-OpSyne '/api/sources/orders-push/ingest' @{
    events = @(@{ external_id = 'event-0001'; payload = ($log | ConvertTo-Json -Compress) })
}
```

`external_id` は観測源内で一意にします。同じID・同じ原本の再送は同じ記録を返し、同じIDで内容が変わるとbatch全体を拒否します。1batch最大500件、原本1件最大1MiB、HTTP要求全体は2,000,000バイトまでです。

### ファイル取込

```powershell
Invoke-OpSyne '/api/sources' @{
    id = 'orders-file'; service_id = 'orders'; name = 'Orders file log'
    kind = 'file'; path = 'C:\logs\orders.jsonl'; stale_after_seconds = 300
}
```

ファイルはOpSyneを動かすユーザーが読めるローカルパスを指定します。登録は管理者のみです。約2秒ごとに改行まで完了した行を取得し、未完の末尾を次回へ残します。原本・取得位置を同時に保存し、回転・切詰めを欠落として記録します。停止期間に失われたファイルやローテーション済み全ファイルを追跡・回収する機構ではありません。

### 変換定義を提案・承認する

次の値の意味が接続先仕様と一致することを確認してから承認します。未定義値は未知になります。

```powershell
$draft = Invoke-OpSyne '/api/adapters' @{
    id = 'orders-json-v1'; name = 'Orders JSON'; source_id = 'orders-push'
    target_instance_id = 'orders-instance-1'; target_version = 1; version = 1
    fields = @{ message = 'message'; outcome = 'result'; severity = 'level' }
    conditions = @{ format = 'orders-v1' }
    outcome_map = @{ error = 'FAILURE'; ok = 'SUCCESS' }
    severity_map = @{ error = 'ERROR'; info = 'INFO' }
}
$draft | ConvertTo-Json -Depth 15
# 最新20件までの保持済み標本を使い、現在の解釈を変更せず候補を検査する。
Invoke-OpSyne '/api/adapters/orders-json-v1/validate' @{}
# 別の承認主体が上の固定内容を確認してから実行する。
Invoke-OpSyne '/api/adapters/orders-json-v1/approve' @{ digest = $draft.digest } $reviewHeaders
Invoke-OpSyne '/api/adapters/orders-json-v1/reprocess' @{}
```

認証付き `POST /api/adapters/{id}/validate` は、固定定義の `digest`、`sample_count`、`supported_count`、最大20件のプレビュー `samples` を返します。現在の解釈には書き込みません。承認には、現在の対象版・適用条件で `KNOWN` または `PARTIAL` になる保持済み標本が1件以上必要です。該当標本がなければ対象の実例を取り込むか定義を修正します。機械検査の成功は値の業務的な意味が正しいことの保証ではなく、人間の確認が必要です。

更新時は新しいIDと版で提案します。適用条件が既存の承認済み定義と重なる場合は承認を拒否します。再解析は解釈と解析状態を更新し、新規配送の消込み・検知・対象操作を発火しません。

### HTTP操作と独立確認を別々に登録する

以下は登録例です。`127.0.0.1:9000` にサービスを自動作成するものではありません。`auth_env` の値を `.env` またはプロセス環境に設定し、必要な接続先だけを登録します。

```powershell
Invoke-OpSyne '/api/services/orders/check' @{
    kind = 'http'; endpoint = 'http://127.0.0.1:9000/health/business'
    expected_status = 200; body_contains = 'ready'; auth_env = 'OPSYNE_HTTP_CHECK_TOKEN'
}
Invoke-OpSyne '/api/capabilities' @{
    id = 'orders-recover'; service_id = 'orders'; name = 'Recover orders processing'
    kind = 'http.request'; version = 1
    endpoint = 'http://127.0.0.1:9000/operations/recover'; method = 'POST'
    body = @{ component = 'orders' }; auth_env = 'OPSYNE_HTTP_OPERATION_TOKEN'
}
Invoke-OpSyne '/api/services/orders/verify' @{}
```

認証が不要な接続先では `auth_env` を省略します。URL埋込みの資格情報、任意shell、SQLを操作として登録しません。HTTPはリダイレクトを追わず、既定5秒・応答64KiBに制限します。GET確認は期待statusと任意の本文部分文字列を評価します。

GET確認で期待していないリダイレクトや401/403/407/408/429を受けた場合は `UNKNOWN` とし、サービス障害と決め付けて操作する入口にしません。確認用の認証不足やレート制限を先に解消します。

計画の作成・承認・実行はデモと同じ画面で進めます。計画は1時間で期限切れになり、登録対象・能力・確認条件の変更では版と承認を再確認します。操作直前の独立確認が `FAIL` のときだけ実行します。`PASS` または確認不能な `UNKNOWN` は実行条件を満たしません。

HTTP 2xxでも業務復旧は未確認です。実行記録から独立確認を行ってください。通信途絶・408・5xx等で操作結果が `UNKNOWN` になると保留を維持します。汎用HTTPの「照合」は正本の操作履歴を取得できないため、`UNKNOWN` を解消せず、再送しません。接続先固有の履歴照合が必要です。

## 5. バックアップと復元

1. 起動端末で `Ctrl+C` を押し、停止完了を待ちます。稼働中のバックアップはロックで拒否します。
2. 新しいバックアップ先を指定します。

```text
python scripts/dev.py run --locked python -m opsyne backup .local/opsyne .local/backups/opsyne-001
python scripts/dev.py run --locked python -m opsyne restore .local/backups/opsyne-001 .local/restored
python scripts/dev.py run --locked python -m opsyne serve --data-dir .local/restored --env-file .env
```

バックアップには4つのDB、署名鍵、ログイントークン、デモ状態を含みます。収集元の外部ファイルと環境変数・`.env` は含みません。バックアップ自体に秘密と原本が入るため、保存先のアクセス権を管理してください。復元先は既存ディレクトリに上書きしません。

復元途中に停止すると `restore.pending` が残り、そのディレクトリからの起動とバックアップを拒否します。マーカーを手で消して起動せず、完全なバックアップから新しいディレクトリへ復元し直します。

復元後は認可世代を更新し、過去の**操作承認・実行許可**の再利用を拒否します。新しい操作許可の発行は、管理者が対象の実状態・外部操作履歴・実行台帳を照合するまで保留します。バックアップ後に実行された操作がないかも確認します。

`owner` のBearer認証で `GET /api/recovery/holds` を読み、照合を完了してから `POST /api/recovery/acknowledge` に `{"reason":"外部操作履歴と復元台帳を照合した具体的な根拠…"}` を送信します。reasonは20文字以上必要です。これで復元保留を解除しても、個別の `UNKNOWN` 操作や古い承認は有効化されません。必要な操作は新しい計画と承認で扱います。

実行台帳が存在しない未送信予約だけは、根拠を付けた管理者の `POST /api/recovery/unsent/{plan_id}` で解消できます。台帳がある操作や汎用HTTPの `UNKNOWN` を解除する経路ではありません。DB行や予約を直接消して再実行しないでください。

## この版の範囲

観測の `healthy` は最近受信できている状態であり、サービス正常やログ完全性の証明ではありません。`UNKNOWN`・解析失敗・欠落を別に表示します。独立確認で解決するのは対象の登録条件を満たした業務障害案件で、SOC案件の侵害根絶は自動確定しません。

同一ホストの保存とワーカー、簡単なルール検知、単一操作計画、宣言的JSON変換を提供します。分散隔離、ホスト停止を検出する外部監視、統計・時系列相関、複数工程の補償処理、保持期限・削除、包括的な秘密分類、接続先全般の履歴照合は今後の範囲です。
