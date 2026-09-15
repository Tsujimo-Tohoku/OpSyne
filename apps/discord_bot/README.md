# OpSyne Discord Bot

本体の開発と並行して作る、独立したDiscordアダプタです。
コード・依存定義・lock・仮想環境・キャッシュ・テストは、このディレクトリ内で管理します。
Botは本体のDB・仮想環境を直接利用せず、専用HTTP APIで接続します。

## 現在できること

- 案件・固定計画のDiscordカードを生成する。状態不明と復旧確認済みを区別する。
- `POST /notifications` で認証済みの通知を受け取り、SQLiteへ保存する。
- 明示した送信コマンドで、登録済みチャンネルにBotとして投稿する。
- HTTP InteractionのEd25519署名・時刻・アプリ・サーバー・チャンネルを検査する。
- Interactionを重複排除し、設定済みbridgeへ署名原文を転送する。
- 確認画面からの承認受付、応答消失、再起動、429待機をローカルの偽接続で検証する。

**本体接続を実装しました。[接続・起動手順](../../docs/discord-setup.md)で設定してください。**
未設定時は「Control APIは未接続」と返します。本体で署名・担当者・対象範囲を再検証し、
本人向けの確認ボタンから承認を記録します。実行と独立確認は本体で行います。
`sync` が本体の案件更新を取り込み、`worker` がDiscordへ配送します。
実Discordではコマンド登録、公開HTTPS経由の署名付き案件照会、本人向けカード表示まで確認済みです。
**自動配信はまだ運用を開始しておらず、実Discordへの通知配送は未検証です。**
通知取込・配送のコードは含みますが、今回の到達点は案件照会です。
Discordでの確認ボタンによる承認・差し戻しも実機では未検証です。
案件ごとのスレッド作成、既存メッセージの更新、OSサービスとしての配備は今後の範囲です。

## 開発環境と検査

Python 3.12 / uvを使います。以下は **`apps/discord_bot` を作業ディレクトリ**として実行します。
`dev.py` は既存のuv実行ファイルを読み取り利用しますが、本体の仮想環境へ同期しません。

```powershell
python dev.py sync --locked --python <Python-3.12の実行ファイル>
python dev.py run --locked python check.py
python dev.py build --no-build-isolation
```

`check.py` はBot専用のRuff lint/format、mypy strict、pytestを実行します。
通常のテストは一時DB、ローカル署名鍵、HTTPX MockTransport/ASGITransportだけで完結します。
実Discord・本体サーバー・LLM・本番資格情報を使いません。

依存追加もこのフォルダで `python dev.py add <package>`、開発用は `add --dev` とします。
lockはuvで生成し、手編集しません。

### 資格情報なしでカードを確認する

```powershell
python dev.py run --locked opsyne-discord preview
python dev.py run --locked opsyne-discord commands
```

`preview` は合成データのカードJSONを端末へ表示し、外部へ送信しません。
表示例の期限は固定された過去日時であり、有効な承認要求ではありません。
`commands` は `/opsyne case id:...` と `/opsyne plan id:...` の登録用JSONを表示します。
コマンド登録自体は行いません。

## Discordへ接続するとき

1. Discord Developer PortalでApplication/Botを作り、対象サーバーにインストールする。
2. 専用チャンネルだけに閲覧・投稿・Embed権限を与える。Administratorは不要。
3. [.env.example](.env.example) を参考に、Botプロセスの環境変数を設定する。設定ファイルは自動読込しない。
4. 下記のHTTPサーバーを起動する。公開する場合はHTTPSのリバースプロキシ等を前段に置き、Interaction Endpoint URLを `/interactions` に向ける。
5. `commands` のJSONをDiscordのguild command APIで登録し、利用できる担当者をサーバー側で設定する。定義の `default_member_permissions: "0"` は既定で利用を制限するための設定。最終的な製品権限はControlが毎回照合する。

```powershell
python dev.py run --locked opsyne-discord serve
```

既定は `127.0.0.1:8766`。`/healthz` はBotの起動状態とbridge設定の有無だけを返し、
Discordへの到達性やControlの正常性を保証しません。
`/notifications` は本体等の信頼できる送信元用です。公開せずネットワークを制限し、専用Bearer資格情報を使います。
Botトークンと通知受付トークン、bridgeトークンは別の値を使ってください。

### 通知を投入して配送する

登録済みチャンネルIDに合わせて [examples/notification.json](examples/notification.json) を編集し、
`Authorization: Bearer <DISCORD_INGEST_TOKEN>` を付けて `POST /notifications` に送ります。
新規保存は202、同一イベントの再受付は200を返します。どちらも返答の `status` は現在の配送状態です。
`PENDING` はDiscordにまだ届いていない状態です。

HTTPサーバーと別のBotプロセスでWorkerを起動すると、保存済み通知を継続配送します。
Ctrl+Cで停止できます。通知をAPIへ投入しただけでは、Workerの起動は行いません。

```powershell
python dev.py run --locked opsyne-discord worker
```

OSのファイルロックにより同じ状態ディレクトリへの送信Workerの二重起動を拒否します。
ロック取得後に、前回中断した送信を `UNKNOWN` にして照合待ちへ戻します。
既定では最大1件の処理ごとに1秒待機し、429の期限も照合します。
接続先やファイル権限などによる異常終了の検出・再起動は、配備先のプロセス監視で設定してください。

単発の送信確認には次を使います。

```powershell
python dev.py run --locked opsyne-discord deliver-once
python dev.py run --locked opsyne-discord queue-status demo-case-204-update-1
```

`deliver-once` は最大1件を送信します。`processed: true` は1件を処理対象にした意味で、
送信成功は `queue-status` の `SENT` と `message_id` で確認します。人の確認済みとは区別します。
`deliver-once` と手動復旧コマンドも同じ送信ロックを取得するため、Worker稼働中は実行を拒否します。

### 停止・送信結果不明

| 状態 | 意味 |
|---|---|
| `PENDING` | 送信待ち、または429の待機中 |
| `SENDING` | 送信の意図を保存済み、結果は未確定 |
| `SENT` | Discord APIからメッセージIDを受領、または独立照合済み |
| `UNKNOWN` | 送信できたか不明。自動再送しない |
| `FAILED` | チャンネル権限不足などの確定失敗 |

429では応答の `retry_after` に従い、全通知を保守的に待機させます。待機期限は再起動後も有効です。
タイムアウト・5xx・不正な成功応答は `UNKNOWN` とし、同じ通知を無条件に再送しません。
資格情報やHTTPエラー本文は保存しません。

送信プロセスが停止していることを確認した後でのみ、次を実行します。

```powershell
python dev.py run --locked opsyne-discord recover-abandoned --sender-stopped
```

残った `SENDING` を `UNKNOWN` にします。Worker起動時はロック取得後に同じ復旧を行います。
Botの生成・HTTP起動時には行わないため、
稼働中の別送信Workerを誤って中断済み扱いにしません。
Discord側で実際に投稿されたメッセージを独立して確認できた場合に限り、次で照合結果を記録します。

```powershell
python dev.py run --locked opsyne-discord reconcile <event_id> <確認済みmessage_id>
```

このコマンド自身はDiscordへの照会や再送を行いません。

## 本体側へ渡す情報

- [Control bridge接続仕様](docs/control-bridge.md): 署名原文、本人照合、固定計画、確認ID、冪等性、応答仕様。
- [ADR: 独立配置と信頼境界](docs/0001-isolated-discord-adapter.md): 今回の配置例外、永続化、採用理由。

本体の計画・認証・通知APIが確定したら、表示データの変換とbridgeの実装を接続テストします。
本体の内部メソッドを直接importしたり、Bot共通管理者トークンで承認を代行したりしません。

## 依存の選定理由

| 依存 | 用途 |
|---|---|
| FastAPI / Pydantic | 小さなHTTP入口と境界JSONの検証 |
| HTTPX | タイムアウト・リダイレクト制限付き通信と、外部接続不要のHTTP試験 |
| PyNaCl | DiscordのEd25519署名検証。独自暗号実装を避ける |
| Uvicorn | 独立したASGIサーバー起動 |
| Ruff / mypy / pytest / Hatchling | lint・型検査・失敗経路試験・配布物ビルド |

## Discord公式仕様

- [署名付きInteractions](https://docs.discord.com/developers/interactions/overview)
- [応答期限と応答形式](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [レート制限](https://docs.discord.com/developers/topics/rate-limits)
- [メッセージとEmbed制限](https://docs.discord.com/developers/resources/message)
