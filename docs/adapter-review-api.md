# 変換定義の承認説明 API（Issue #4）

バックエンドと承認画面が共通で使用する受け渡し仕様。画面の表示・作成・説明編集・承認を接続済み。
実装上の判断は [ADR 0004](adr/0004-adapter-review-explanations.md) を参照。

## 読み出し

`GET /api/adapters/{id}` は認証済み利用者が参照できる。`GET /api/overview` の
`adapters[]` も同じレコード形式。既存の定義フィールドに次のフィールドが加わる。
以下は追加部分の例であり、実際の応答は同じオブジェクトにid、source_id、対象実体・版、
fields、conditions、status、proposer、digestなどの既存フィールドも含む。

```json
{
  "review_schema_version": 1,
  "explanation_revision": 2,
  "explanation": {
    "human": {
      "monitoring_purpose": "決済障害を早期に把握する",
      "expected_insights": ["処理失敗の発生"],
      "rationale": ["決済サービスの運用監視要件"],
      "questions": ["重大度の業務上の意味を確認する"]
    },
    "recorded_by": "operator",
    "agent": {
      "task_id": "task-example",
      "suggested_use": "処理結果の調査に利用できる可能性がある",
      "rationale": [{"text": "結果を表すキーがある", "evidence_ids": ["raw-example"]}],
      "unknowns": ["数値コードの意味は判断できない"]
    }
  },
  "explanation_editors": ["operator"]
}
```

- `human.monitoring_purpose`: 利用者の監視目的。nullは未記録。
- `human.expected_insights`: 利用者が変換によって把握したいこと。
- `human.rationale` / `questions`: 利用者の根拠・確認事項。
- `agent.suggested_use`: Agentが提案した用途と把握できそうなこと。利用者の目的と区別して表示する。
- `agent.rationale`: 当該定義を提案した際の根拠と原本の参照id。
- `agent.unknowns`: Agentが判断できないこと・確認事項。
- `recorded_by` は人間の説明の最終記録者。`task_id` はAgent説明の生成タスク。
- `human` / `agent` はそれぞれnullになり得る。説明なしの既存レコードでは、追加フィールド自体が
  ない。欠落・nullは未記録、配列の空は記録なしとして表示する。Agent用途による目的の補完は禁止。
- 説明はプレーンテキストとして表示する。ログやAgent出力に含まれるHTML・命令を実行しない。

## 作成・更新

`POST /api/adapters` の従来の定義JSONに任意の `explanation` を追加できる。
入力の `explanation` は上記の **humanの中身** であり、応答のラッパーとは異なる。
省略した既存クライアントも動作する。記録者やAgent説明はクライアントから指定できない。

`PUT /api/adapters/{id}/explanation` はadmin/operatorのみ。DRAFTの人間の説明全体を置換する。
省略した配列は空、目的はnullになる。Agent説明・変換規則は更新されない。

```json
{
  "digest": "画面に表示した定義の64桁のSHA-256値",
  "explanation": {
    "monitoring_purpose": "決済障害を早期に把握する",
    "expected_insights": ["処理失敗の発生"],
    "rationale": [],
    "questions": ["重大度の意味を確認する"]
  }
}
```

成功時は更新済みの定義レコードを返す。目的・各配列要素は空白だけの文字列不可、最大2000文字。
配列上限はexpected_insights/rationaleが30件、questionsが50件。
目的を未記録に戻すときはnullを指定する。説明更新も監査対象であり、旧版を保持する。

## 承認時の扱い

`POST /api/adapters/{id}/approve` の形式は従来どおり `{"digest":"…"}`。
画面に表示したレコードのdigestを送信する。更新後のdigestを再取得して見せずに自動送信しない。
説明更新によりdigestが変わるため、別画面での編集・同時編集後の古い承認は409になる。
409時は再読み込みして定義と説明を再確認する。承認前の標本検証・対象版検査も引き続き行う。

| HTTP | 意味 |
| --- | --- |
| 401 | 認証なし・無効 |
| 403 | 権限不足、提案者/説明編集者による自己承認、保存内容の整合性不一致 |
| 404 | 指定idなし |
| 409 | 古いdigest、DRAFT以外の説明更新・承認、既存の範囲競合など |
| 422 | 入力形式・文字数・未知フィールドなどの違反 |

定義の `version` は変換規則の版、`explanation_revision` は承認説明の改訂番号。
説明だけを編集しても自動提案グループのadapter_idや標本参照を変更しない。
承認済み定義の説明を変更する場合は、新しいid/versionで提案し直す。
