# Fixtures de Codex CLI

Ambos capturados **reales** de `codex exec --json` 0.157.1 (2026-09-27):
- `codex_turn_failed_auth.jsonl`: token revocado → `turn.failed` 401.
- `codex_turn_ok.jsonl`: decisión válida. Nótese `input_tokens≈14k` para un prompt trivial: es el
  sobrecoste fijo de Codex CLI por llamada (su propio system prompt y tools).
