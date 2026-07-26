# Tkn Codex Context Pipeline

Codex appのProject状態と`~/.codex/sessions`を読み、chatを再利用可能な
Session Note v2へ変換する、独立したローカルデータパイプラインです。
Project folderにはmarker・設定・contextを一切書きません。

## 対象範囲

初版はsession summary生成だけを扱います。decision、current working context、
global contextは対象外です。出力先は従来どおりです。

```text
~/.tkn/codex-context/state/<projectId>/sessions/
```

Codex app ProjectのPrimary rootとSecondary rootは、どちらも現在有効なrootとして
扱います。Secondaryをhistorical rootとはみなしません。複数rootが異なるGit
repositoryでも問題ありません。

## 開発・インストール

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

uv toolとしてインストールできます。

```powershell
uv tool install .
tkn-codex-context --help
```

## 設定

最初にグローバル設定と通常runのwatermarkを作ります。

```powershell
tkn-codex-context config init
tkn-codex-context config show
```

設定の優先順位は次のとおりです。

1. built-in defaults
2. `~/.tkn/codex-context-pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. `--config`
5. CLI option

commitするのは`.tkn/config.example.yaml`だけです。実設定をcommitしないで
ください。YAML中の相対パスは、そのYAMLファイルがあるfolderを基準に解決します。

## 安全な初回実行

最初に読み取り専用dry-runを確認します。dry-runではregistry、note、refresh
state、cache、reportのいずれも変更しません。

```powershell
tkn-codex-context projects sync --dry-run
tkn-codex-context session-notes run --dry-run
```

確認後に反映します。

```powershell
tkn-codex-context projects sync
tkn-codex-context session-notes run
```

通常runは`installed_at`以後で、30分以上idleのchatだけを処理します。
過去分は明示的に実行します。

```powershell
tkn-codex-context session-notes backfill --project-id <projectId> --dry-run
tkn-codex-context session-notes backfill --all
tkn-codex-context session-notes rebuild --project-id <projectId> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectId> --force
tkn-codex-context validate <session-note.md>
```

全コマンドはJSON結果を出力します。`--verbose`は進捗ログを追加し、
`--quiet`はJSONを維持したままログを抑えます。

## Projectとthreadの同定

既存registryの`projectId`を正本とします。Codex app Projectとの初回bindingは、
保存済みsource binding、一意なroot完全一致、一意なProject名完全一致、新規
context Projectの決定論的作成、の順です。衝突はpendingとして報告し、noteを
生成しません。

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
