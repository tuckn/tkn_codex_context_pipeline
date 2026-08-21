# AGENTS.md

## Test temporary directories

- Prefer framework-managed or operating-system temporary directories.
- If Windows ACLs or the sandbox prevent their use, use only a fresh dedicated
  directory under `.tmp/<tool>/<run-id>/` and remove it after the test run.
- A pytest `--basetemp` must target that dedicated child directory, never the
  repository root or `.tmp` itself.
- Do not create ad hoc `.test-tmp-*` directories. If cleanup fails, report the exact
  path and do not create additional fallback directories.
