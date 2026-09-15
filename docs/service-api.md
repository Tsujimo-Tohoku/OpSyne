# サービス別API契約（Issue #6）

全ルートは既存のBearer認証を使い、viewerを含む既存の利用者が参照できる。
サービス選択による権限の追加はない。既存の `/api/overview` は互換性のため変更しない。

## サマリー

`GET /api/services/{service_id}/overview`

```json
{
  "service": {"id":"checkout","name":"Checkout","instance_id":"checkout-v1","version":1,"owner":"ops","criticality":"medium","enabled":true},
  "unresolved_incidents": 12,
  "pending_plans": 3,
  "pending_adapters": 2,
  "reviewable_by_actor": {"plan":2,"adapter":1},
  "coverage": [],
  "observed_at": 1789440000.0,
  "history_unknown_count": 10
}
```

- `unresolved_incidents`: サービスに属するcaseのうち、statusがRESOLVEDでない全件。
  解釈・観測カバレッジの案件も含む。ページの表示件数とは独立。
- `pending_plans` / `pending_adapters`: 当該サービスのDRAFT全件。期限切れのDRAFTも含む。
  この数字だけで「今承認できる」と判断しない。
- `reviewable_by_actor`: admin/approverかつ本人が提案者・説明編集者でないDRAFTの件数。
  対象版・期限・標本・範囲競合の認可チェックは含まない。
- `coverage`: 当該サービスの観測源の既存Coverage契約をそのまま返す。
  空配列は観測源なし、missingはログ未受信であり、正常の意味ではない。
- `history_unknown_count`: **全体で**所属が確認できない監査の件数。サービスに割り当てない。
- `observed_at`: 取得時刻。複数DBをまたぐ厳密な同時点のスナップショットではない。

## 一覧とページング

`GET /api/services/{service_id}/items/{collection}?limit=50&cursor=...`

| collection | itemsの内容 |
| --- | --- |
| cases | 当該サービスのCase（解決済みも含む） |
| approvals | DRAFTの対応案・変換定義。下記ラッパー形式 |
| executions | 当該サービスのExecution。verificationは未確認ならnull |
| history | サービスとの対応が記録済みの監査イベント |

```json
{
  "items": [],
  "total": 0,
  "has_more": false,
  "next_cursor": null,
  "collection_state": "empty",
  "observed_at": 1789440000.0
}
```

`limit` は1〜200、既定50。フィルター後の `total` を返す。
`next_cursor` は不透明な文字列としてURLエンコードし、同じサービス・一覧・主体・limitで送信する。
順序はid降順（履歴は数値ID）、承認待ちはkindとidの降順。作成日時順とは限らない。
`has_more=false` で継続は終わる。`collection_state` は、この応答1ページについて、
0件ならempty、全件を含めばcomplete、一部だけならpartial。最終ページでもpartialになり得る。
通信失敗は「未取得」として扱い、0件に置き換えない。

カーソル発行後に対象一覧の追加・削除・更新があれば409。画面は蓄積中のページを破棄し、
カーソルなしで再取得する。無変更ならページ間で欠落・重複なく取得できる。
別サービスの変更はそのサービスだけの一覧の継続を妨げない。
サマリーと一覧は別要求であり、更新中は件数が変わり得る。

### 承認待ち

```json
{
  "kind": "adapter",
  "id": "adapter-example",
  "definition": {"id":"adapter-example","status":"DRAFT","proposer":"agent:adapter"},
  "review": {"can_review":true,"reason":null,"authorization_required":true}
}
```

上記definitionは抜粋。実際には既存のPlan/Adapterレコードを全体で返し、digest、対象・版、
根拠などを落とさない。kindはplanまたはadapter。`can_review=false` のreasonはrole（役割不足）、
self_authored（提案者・説明編集者本人）。これはレビュー可能な主体を判別する情報であって、
承認の成功を保証しない。承認には従来のAPIへ表示したdigestを送り、サーバーで再認可する。
PR #8の説明フィールドがある場合も保持し、その編集者を自己承認判定に含める。

## 操作履歴

サービス別historyの各行は `id, at, actor, action, object_id, detail` に加え、
`service_id, object_kind, scope, current_object, related_plan` を返す。
scopeはservice。object_kindはplan/adapter/execution/case/source等で、対象を区別できる。
`current_object` は現在の対象レコード、実行なら `related_plan` で提案者・承認者・対象実体・版へ
たどれる。対象が見つからなければnull。現在の対象レコードと監査時点の状態を混同しない。
監査時点の行は不変で、例えばexecution.verifyのdetailは確認時のPASS/FAIL/UNKNOWN。
ExecutionのSUCCEEDEDとverificationのPASSは別々に表示し、未確認/null・UNKNOWNを成功にしない。

`GET /api/history?scope=global|unknown&limit=50&cursor=...` は同じページ形式。
globalは既知の共通操作、unknownは旧記録や所属を確定できない記録。両者のservice_idはnull。
既存のobject_idや文章がサービス名と一致しても割り当てない。UIにはサービス別履歴とは別に、
共通・所属不明の記録を確認できる導線を設ける。

## エラーと互換性

| HTTP | 意味 |
| --- | --- |
| 401 | 認証なし・無効 |
| 404 | サービス不存在（空一覧ではない） |
| 400 | カーソル破損・署名不正・対象/主体/limitの不一致 |
| 409 | 対象一覧が変わったため先頭から再取得が必要 |
| 422 | collection/scope/limit等の入力違反 |

エラーの本文は既存APIどおり `{"detail":"..."}`（入力検証のdetailは配列）。
新テーブルは起動時に追加する。旧監査データを削除・推測で移行しない。
実行・照合・承認・バックアップの手順は従来どおり。詳細は [ADR 0005](adr/0005-service-scoped-queries.md)。
