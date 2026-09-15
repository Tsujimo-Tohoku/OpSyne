# Control bridge v1 接続提案

**この文書はBot側の接続案であり、本体に実装済みのAPI仕様ではありません。** Bot側コードは [bridge.py](../src/opsyne_discord/bridge.py)、[app.py](../src/opsyne_discord/app.py)、[presentation.py](../src/opsyne_discord/presentation.py) を参照してください。

## 1. 接続先と認証

`DISCORD_CONTROL_BRIDGE_URL` に設定した単一のURLへPOSTする。既定のルート名は定めず、Interaction本文のURL・引数から送信先を選ばない。URLと `DISCORD_CONTROL_BRIDGE_TOKEN` は両方設定するか、両方空にする。未設定なら承認・実行を行わない旨を返す。

送信はHTTPSに限定する。HTTPはループバック試験だけを許可し、リダイレクトを追跡しない。ヘッダーは `Authorization: Bearer <DISCORD_CONTROL_BRIDGE_TOKEN>`。この資格情報は限定したbridgeへのアクセス用であり、管理者権限や人の承認を表さない。

## 2. 要求JSON

```json
{
  "protocol": "opsyne-discord/v1",
  "raw_body_base64": "<Discordから受信した原文バイト列のBase64>",
  "signature_ed25519": "<X-Signature-Ed25519>",
  "signature_timestamp": "<X-Signature-Timestamp>"
}
```

Botが認識する操作は `/opsyne case id:<参照ID>`、`/opsyne plan id:<参照ID>`、コンポーネントの `review:<approval_request_id>`、`reject:<approval_request_id>`、`confirm:<confirmation_id>`。コンポーネント参照は `[A-Za-z0-9_-]{1,80}`。操作名やユーザー名を別の信頼済みフィールドとして送らず、bridge自身が署名対象の原文から取り出す。任意の `role`、`actor`、実行コマンド、権限上書き引数は契約に含めない。

### bridge側の必須検証

1. Base64を復元した**元のバイト列**について、設定済みDiscord公開鍵で `signature_timestamp` のASCIIバイト列と原文を連結した署名対象を検証する。JSONの再シリアライズ結果では検証しない。署名時刻も現在時刻と照合する。Botの許容差は前後300秒であり、bridgeでも同等以下の期限を適用する。
2. 署名内の `application_id`、`guild_id`、`channel_id`、人の `member.user.id`、Interaction IDを検証する。DM、Botユーザー、未登録の文脈は拒否する。Discordの表示名やロールだけで製品の権限を付与しない。
3. Discord User IDから事前に本人確認・登録したOpSyneの主体を特定し、現在の対象範囲・閲覧権限・承認資格を照合する。通知・照会内容も送信先と本人が閲覧できる範囲へ限定する。
4. 型、サブコマンドの引数、参照先、許可された操作を厳密に検証する。Bot側の構文検査だけに依存しない。
5. Interaction IDと原文ハッシュ、処理状態、結果を耐久保存する。同じIDの同じ内容は既存結果へ収束させ、異なる内容は拒否する。Botの受領記録が失われても二重承認・二重実行しない。

署名原文にはDiscord Interactionトークンが含まれる。原文はTLSで一時的に転送し、リバースプロキシ・アプリ・APM・例外出力を含め**ログや永続ストアへ保存しない**。監査には許可された識別子、原文ハッシュ、検証・判断結果を残す。

## 3. 計画の確認から承認まで

`review` は現在の承認要求を照会する操作であり、承認を記録しない。bridgeは固定計画の全文、対象実体・版、digest、現在の資格と期限を照合し、本人向けの計画と `confirmation_id` を返す。

`confirmation_id` は推測困難、短命、1回限りの参照とし、確認した本人、アプリ・guild・channel、計画ID・固定内容のdigest、対象実体・版、期限に結び付けてControl側へ保存する。`confirm` で新たに届いた署名を検証し、その本人と保存済みの固定内容が一致することを再確認してから承認を記録する。計画変更、対象置換、失効、期限切れ、権限不足、競合、確認不能は拒否する。

`reject` も現在の権限と承認要求への結び付きを検証する。返信の欠落は処理未実施の証明にならない。Control側にはInteraction IDによる受付結果照会が必要だが、その照会ルートとBotからの自動照会はこのv1では未実装。現在は案件の最新状態を照会して確認する。

承認は実行許可や復旧確認を意味しない。承認後の現在条件照合、操作前台帳、Runner、独立した結果確認は本体の責任とする。

## 4. 応答JSON

全応答はHTTP 2xxのJSONとし、`protocol` は `opsyne-discord/v1`。トップレベルの許可フィールドは `protocol`、`kind`、`case`、`plan`、`confirmation_id` のみ。後ろ3つは未使用なら省略またはnullとする。

| `kind` | 必要な内容 | Botの表示・処理 |
|---|---|---|
| `case` | `case` | 案件を本人向けに表示 |
| `plan` | `plan` | 固定計画と確認・差し戻しボタンを表示 |
| `review` | `plan`、`confirmation_id` | `review` 要求への応答のみ。本人向け計画と `confirm` ボタンを表示 |
| `approval_recorded` | 追加フィールドなし | `confirm` 要求への応答のみ。Controlへの承認記録を表示 |
| `rejected` | 追加フィールドなし | 拒否または差し戻し済みと表示 |
| `pending` | 追加フィールドなし | 処理中・承認確定未確認と表示 |
| `unavailable` | 追加フィールドなし | 状態確認不能・承認済みとは扱わない |

### 表示データ

`case` は `case_id`、`title`、`state`、`summary`、`observed_at`（すべて文字列）、`evidence_refs`（文字列配列、省略時は空）。状態が未知・欠落・`UNKNOWN`なら復旧済みと表示しない。

`plan` は全フィールド必須:

| フィールド | 型・意味 |
|---|---|
| `plan_id` | 文字列。固定計画の参照 |
| `version` | 1以上の整数。Bot表示モデルの版 |
| `digest` | 文字列。固定計画内容のdigest |
| `target_id`, `target_version` | 文字列。対象実体とその版 |
| `operation`, `impact` | 文字列。操作と影響 |
| `preconditions`, `success_conditions`, `abort_conditions` | 空でない文字列配列。該当条件がない場合も理由を明記 |
| `expires_at` | タイムゾーン付きISO 8601日時文字列 |
| `approval_request_id` | 現在の承認要求を照会する参照。`[A-Za-z0-9_-]{1,80}` |

Botは計画を全文表示できない場合、必須情報が欠ける場合、日時等が不正な場合に操作ボタンを付けない。期限の現在照合はbridgeが行う。

**本体との変換は未確定。** 現在確認した本体の `Plan` は計画独自の `version` を持たず、IDとdigestで固定内容を扱う。Botの `PlanView.version` に `target_version` を流用したり、値を推測したりしない。`preconditions` 等も含め、統合時に根拠ある変換または表示契約の改訂を合意・試験する。

### 不明・不正・遅延時

Botのbridge待機はHTTPタイムアウト1.5秒、処理全体1.8秒。タイムアウト、HTTPエラー、不正なJSON・応答種別では受付結果不明と返し、POSTを自動再送しない。古い確認IDや権限不足はbridgeが承認せず `rejected` 等で返す。`approval_recorded` は承認が耐久保存された後に限って返す。

## 5. 本体からBotへの通知

Bot側の実装済み受付は `POST /notifications`。`Authorization: Bearer <DISCORD_INGEST_TOKEN>` を要求し、通知JSONは次の形式:

```json
{
  "event_id": "<同一通知の再送でも変わらない一意ID>",
  "channel_id": "<登録済みDiscordチャンネルID>",
  "case": {
    "case_id": "DEMO-204",
    "title": "異常を検知",
    "state": "OPEN",
    "summary": "合成データの通知例です。",
    "observed_at": "2026-09-15T14:32:00+09:00",
    "evidence_refs": ["synthetic-evidence-1"]
  }
}
```

`case` または `plan` のどちらか一つを指定する。`event_id` は1〜200文字、`channel_id` は1〜20桁の数字文字列。本文は65,536バイト以下。認証失敗は401、未登録チャンネルは403、不正な通知は422、同じ通知IDの内容・送信先競合は409、保存不能は503。新規保存後は `202 {"status":"PENDING","event_id":"..."}` を返し、Discord送信済み・人の確認済みを意味しない。同じIDと同じ表示内容・送信先の再受付は重複行を作らず、200と現在の配送状態を返す。並行するWorkerにより応答時点で状態が進んでいる場合もある。

配送は `worker` が継続処理し、`deliver-once` は最大1件を担当する。同じ状態ディレクトリへの送信はOSファイルロックで排他化する。明確な429は応答受領から指定待機時間後に再試行し、通信障害・不確かな成功応答・5xx等は `UNKNOWN` として自動再送しない。Workerは起動時に排他ロック取得後、残った `SENDING` を `UNKNOWN` にする。独立照合は配送用CLIを使う。

現在、本体の案件更新とこの通知受付にトランザクション上の結合はない。本体側で案件変更と通知送信待ちを同じトランザクションへ保存し、再送時にも一意IDと内容を維持する接続が必要。BotのSQLiteだけでは、本体から届く前の通知喪失を防げない。案件スレッド作成、既存メッセージ更新、本体からの自動配送開始もこの接続案の実装済み範囲には含めない。
