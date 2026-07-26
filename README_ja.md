# Tkn Codex Context Pipeline

Codex appのProject状態と`~/.codex/sessions`を読み、chatを再利用可能な
Session Note v2へ変換する、独立したローカルデータパイプラインです。
Project folderにはmarker・設定・contextを一切書きません。

## インストール

Python 3.11以上とuvを用意し、このrepositoryのrootでCLIをインストールします。

```powershell
uv tool install .
tkn-codex-context --help
```

## 設定

最初にCodex app Projectと保存先をdry-runで確認し、pipelineを初期化します。

```powershell
tkn-codex-context init --dry-run
tkn-codex-context init
tkn-codex-context config show
```

`init`はグローバル設定とProject registryを作成し、Codex app左ペインの
Projectごとに空の`sessions/`を用意します。Session Noteは生成しません。
実行日時は`installed_at`として保存され、通常実行が
自動処理するのは、この日時以後に作成または更新されたchatだけです。それ以前の
chatは、明示的な`backfill`または`rebuild`で処理します。

既存のpipelineを完全に作り直す場合は、まず削除対象を確認してからforce初期化
します。modelや保存先などの設定は維持され、`installed_at`だけが更新されます。

```powershell
tkn-codex-context init --force --dry-run
tkn-codex-context init --force
```

アプリ自身のファイルは、用途別に次の場所へ保存します。

```text
~/.tkn/codex_context_pipeline/
├── config.yaml
├── data/
│   ├── project-registry.jsonl
│   └── projects/
│       └── <projectId>/
│           └── sessions/
└── state/
    ├── projects/
    │   └── <projectId>/
    │       └── chat-refresh-state.json
    └── reports/

~/.cache/codex_context_pipeline/
└── 中断した処理を再開するためのcache
```

`data/`はProject registryとSession Noteなどの永続データ、`state/`はrefresh
checkpointやrun reportなどの再作成可能な状態・履歴に使用します。
cacheは既定で`~/.cache`へ分離し、`XDG_CACHE_HOME`が設定されている環境では
その場所を使用します。実行中だけ必要なmodel入出力などは、Pythonの標準一時
directoryを使うため、Windowsでは通常`%TMP%`、Linuxでは通常`/tmp`に置かれます。

設定の優先順位は次のとおりです。

1. built-in defaults
2. `~/.tkn/codex_context_pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. `--config`
5. CLI option

commitするのは`.tkn/config.example.yaml`だけです。実設定をcommitしないで
ください。YAML中の相対パスは、そのYAMLファイルがあるfolderを基準に解決します。

## 通常実行

初期化後にCodex app Projectが追加・変更された場合は同期します。dry-runでは
registry、note、refresh state、cache、reportのいずれも変更しません。

```powershell
tkn-codex-context projects sync --dry-run
tkn-codex-context session-notes run --dry-run
```

確認後に反映します。

```powershell
tkn-codex-context projects sync
tkn-codex-context session-notes run
```

通常runは30分以上idleのchatだけを処理します。過去分は明示的に実行します。

```powershell
tkn-codex-context session-notes backfill --project-id <projectId> --dry-run
tkn-codex-context session-notes backfill --all
tkn-codex-context session-notes rebuild --project-id <projectId> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectId> --force
tkn-codex-context validate <session-note.md>
```

全コマンドはJSON結果を出力します。`--verbose`は進捗ログを追加し、
`--quiet`はJSONを維持したままログを抑えます。

## 対象範囲

初版はsession summary生成だけを扱います。decision、current working context、
global contextは対象外です。

Codex app ProjectのPrimary rootとSecondary rootは、どちらも現在有効なrootとして
扱います。Secondaryをhistorical rootとはみなしません。複数rootが異なるGit
repositoryでも問題ありません。

## Projectとthreadの同定

`projectId`には、Codex appの`local-projects`に保存された内部Project IDを
そのまま使用します。Project名とrootは変更可能なmetadataであり、Projectの
同一性判定には使用しません。同じrootでも左ペイン上で別Projectなら別Project、
同じ内部IDなら名前やrootが変わっても同じProjectとして扱います。

threadは、Codex appの明示assignment、全active rootに対する一意なcwd一致、
保存済みhistorical aliasの順で同定します。projectlessまたはambiguousなthreadは
除外してreportへ残します。

## Codex内部形式への依存

`.codex-global-state.json`とJSONL sessionはCodex appのprivateな内部形式であり、
公開された互換contractではありません。adapterは必要な構造を厳密に検証し、
破損・欠落・互換性のない変更を検出した場合は推測せずfail closedします。
元JSONLは常にread-onlyです。

生成にはephemeral・read-only sandbox・structured outputのCodex CLIを使います。
source/generator fingerprintが同じthreadはモデルを呼ばずno-opにします。noteと
refresh stateは検証後にatomic反映し、通常runとrebuildの中断済み生成物は再開に
利用します。

## 開発

開発用dependencyを同期して、test・静的検査・buildを実行します。

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```
