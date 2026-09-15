# ADR 0007: 承認後の復旧ワーカー

承認専用ユーザーも `POST /api/plans/{id}/approve-and-execute` に確認したdigestを
送れる。既存の承認検証と復旧ジョブ保存を同一トランザクションで行う。
従来の `/approve` は承認のみ。同一承認者・digestの再送は既存ジョブを返す。

内部実行主体 `system:recovery` は公開トークンを持たず、保存されたplan ID・digest・
承認者を照合したジョブだけを実行する。approverの直接execute権限は拡張しない。
既存の現在条件照合、署名許可、外部操作前の耐久台帳を通す。

ジョブはQUEUED→EXECUTING→VERIFYING→COMPLETED。UNKNOWNは再送せず保留。
操作成功後の確認FAIL/UNKNOWNはCHECK_FAILED。再起動後、実行済み台帳があれば
操作を繰り返さず確認する。実行開始中の停止で台帳がなければBLOCKEDにする。

進捗は `GET /api/plans/{id}/recovery`、サービス一覧は
`GET /api/services/{id}/recoveries`。案件APIにもrecoveriesとrecovery_proposalを追加。
失敗時の再確認・照合は既存のverify/reconcile APIを利用する。

AIは供給された証拠と登録操作のID・版・名称、独立確認条件から候補を選ぶ。
対象・操作・証拠を再照合し、AI taskをproposerと来歴へ記録する。
同一案件・対象版・証拠の重複提案は既存計画に対応付ける。候補なしは提案保留。
旧Planの署名対象フィールドは変更しない。

合成AI応答の主要テストを実施。実LLM・実サービスの検証は別途行う。
