### session-notes build：RawからSession Noteを生成

会話ログをRawとして保存・正規化し、対象会話のSession NoteをMarkdownで生成します。
`--thread-id` で1会話を選べます。DecisionとWorking Contextは、このコマンドでは生成しません。
AIにはイベント内容・ID、生成指示、出力スキーマを渡します。

以下は `tkn-genai-chat-note session-notes build` の処理です。表の略記は直後の図で使います。

| 図中の表記 | 設定項目 | 既定の保存先 |
| --- | --- | --- |
| `C` | `chat.providers.codex.home` | `~/.codex` |
| `R` | `raw_root` | `~/.tkn/genai_chat_note_pipeline/raw` |
| `D` | `data_root` | `~/.tkn/genai_chat_note_pipeline/data` |
| `S` | `state_root` | `~/.tkn/genai_chat_note_pipeline/state` |

`T` は会話の `threadKey`、`H` は内容のハッシュです。
図のパスでは、それぞれの実際の値を表すプレースホルダーとして使います。

```mermaid
sequenceDiagram
    autonumber
    actor U as 利用者・定期実行
    participant P as パイプラインCLI
    participant C as Codex保存領域
    participant F as 保存先 R・D・S
    participant AI as 生成AI

    U->>P: session-notes build
    P->>P: config.yamlを読み込む<br/>保存先・取得元ID・モデル
    P->>F: S/codex/{sourceId}/ledger.jsonなどを読み込む<br/>前回の処理状態を確認

    P->>C: C/sessions/**/*.jsonl<br/>C/archived_sessions/**/*.jsonl
    C-->>P: 会話ログの元のバイト列
    P->>F: R/codex/{sourceId}/sessions/YYYY/MM/DD/rollout-*.jsonl<br/>元の内容を変更せず保存
    P->>F: R/codex/{sourceId}/manifest.jsonl<br/>取得元・日時・ハッシュを記録

    opt Project所属情報を取得できる場合
        P->>C: C/.codex-global-state.json
        C-->>P: Project情報・会話の所属
        P->>F: R/codex/{sourceId}/metadata/H.json<br/>所属情報のスナップショット
    end

    P->>P: Rawを解析・イベントを正規化<br/>会話ID・発言・時刻・原文の行参照
    P->>F: D/codex/{sourceId}/source-aligned/T/H.json<br/>Canonical Eventsを保存

    loop 新規・変更・未完了の対象会話
        P->>P: 要約対象のイベントを準備<br/>長い会話は分割
        P->>AI: 会話ID＋イベント内容＋イベントID<br/>生成指示＋出力スキーマ
        AI-->>P: 部分記録のJSON<br/>時系列本文＋概要＋根拠ID
        opt 分割した場合
            P->>AI: 部分記録から概要・終了状態を統合
            AI-->>P: 概要・終了状態のJSON
        end
        P->>P: 時系列は部分記録を保持して結合<br/>日時・主体・根拠を検証してMarkdownへ
        P->>F: D/codex/{sourceId}/session-notes/YYYY/MM/*.md<br/>Session Noteを保存
        P->>F: 来歴と処理チェックポイントを記録
    end
```

保存するCanonical Eventsと要約処理が使うイベントは、同じ解析結果に基づきます。
現在の実装は、保存した正規化JSONを再読込せず、メモリー上のイベントを要約処理へ渡します。
この段階の要約単位は会話であり、作業scopeによる統合とは独立しています。
