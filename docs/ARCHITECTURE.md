# Architecture Document — AI-Powered PR Code Review

## 1. Overview

This system automatically reviews Pull Requests on GitHub by analyzing code diffs with an LLM (OpenAI GPT-4o) and posting structured feedback as PR comments.

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────┐
│  PR Opened   │────▶│  GitHub Actions    │────▶│  review.py   │
│  (trigger)   │     │  Workflow          │     │  (Python)    │
└──────────────┘     └───────────────────┘     └──────┬───────┘
                                                       │
                                          ┌────────────┼────────────┐
                                          ▼            ▼            ▼
                                     ┌─────────┐ ┌─────────┐ ┌──────────┐
                                     │ Get Diff │ │ OpenAI  │ │ Post     │
                                     │ (GitHub  │ │ API     │ │ Comment  │
                                     │  API)    │ │         │ │ (GitHub) │
                                     └─────────┘ └─────────┘ └──────────┘
```

## 2. Components

### 2.1 GitHub Actions Workflow (`.github/workflows/ai-review.yml`)

**Responsibility:** Orchestration — triggers on PR events, sets up the environment, and runs the review script.

**Design decisions:**
- **`pull_request` event** (not `pull_request_target`): Runs in the context of the fork/branch without access to repository secrets for fork PRs. This is a deliberate security choice (see Section 5).
- **Event types `opened`, `synchronize`, `reopened`**: Covers new PRs and subsequent pushes. Avoids re-reviewing on label/assignment changes.
- **Draft PR skip**: Uses `if: github.event.pull_request.draft == false` to avoid wasting API calls on work-in-progress.
- **`fetch-depth: 0`**: Full git history is needed for accurate diff computation.
- **Pip caching**: `cache: "pip"` in setup-python speeds up dependency installation.

### 2.2 Review Script (`scripts/review.py`)

**Responsibility:** Core logic — fetches diff, filters/chunks files, calls OpenAI, and posts results.

**Key modules:**

| Function | Purpose |
|---|---|
| `get_pr_diff()` | Fetches diff via PyGithub's compare API |
| `parse_diff()` | Parses unified diff into per-file segments |
| `should_review_file()` | Filters by extension and exclude patterns |
| `chunk_diff()` | Groups files into token-bounded chunks |
| `review_chunk()` | Sends a chunk to OpenAI with structured output (with retry) |
| `retry_with_backoff()` | Exponential backoff wrapper for transient API failures |
| `_review_chunks_parallel()` | Concurrent chunk review via ThreadPoolExecutor |
| `merge_reviews()` | Combines results from multiple chunks |
| `format_review_comment()` | Renders Markdown table + detailed suggestions |
| `post_review_comment()` | Posts the summary as a PR issue comment |
| `post_inline_comments()` | Posts HIGH-severity issues as inline review comments |

**Design decisions:**
- **Structured Outputs** (`response_format` with JSON Schema): Guarantees 100% schema compliance from the model. No need for output parsing/validation retries.
- **Per-file chunking with hunk-level fallback**: First splits by file; if a single file exceeds the token limit, splits by `@@` hunk boundaries. This preserves context while staying within limits.
- **tiktoken for token counting**: Accurate pre-flight token estimation prevents API errors from oversized requests.
- **Retry with exponential backoff**: Transient API errors (rate limits, timeouts) are retried automatically with configurable attempt count and delay.
- **Parallel chunk processing**: Multiple chunks are reviewed concurrently via `ThreadPoolExecutor`, reducing total review time for large PRs. Concurrency is configurable via `MAX_CONCURRENT_REVIEWS`.
- **Environment-variable configuration**: All key settings (model, chunk size, concurrency, language, retry params) are overridable via environment variables without code changes.

### 2.3 System Prompt (`scripts/prompts/system_prompt.txt`)

**Responsibility:** Defines the review persona, criteria, severity levels, and output format expectations.

**Design decisions:**
- **Externalized prompt file**: Easier to iterate on prompt engineering without changing code.
- **5 review categories**: Readability, Performance, Security, Best Practices, Bug Risk — aligned with industry-standard review checklists.
- **Explicit severity definitions**: Reduces ambiguity in LLM output (e.g., "high" = production-impacting bugs/vulnerabilities).
- **Anti-instruction-injection clause**: The prompt explicitly instructs the model to ignore any directives embedded in the diff.

## 3. Data Flow

```
1. PR event triggers workflow
2. Workflow checks out code, installs dependencies
3. review.py starts:
   a. Fetches PR diff via GitHub API (PyGithub)
   b. Parses diff into per-file segments
   c. Filters out non-reviewable files (lock files, binaries, etc.)
   d. Chunks remaining diffs to fit within token limits
   e. For each chunk: sends to OpenAI API with system prompt
   f. Merges all chunk results into a single review
   g. Formats results as Markdown
   h. Posts summary comment on PR
   i. Posts inline comments for HIGH severity issues
```

## 4. Performance & Optimization

### Large PRs (1000+ lines)

- **File-level splitting**: Each file's diff is reviewed independently.
- **Parallel chunk processing**: Multiple chunks are reviewed concurrently via `ThreadPoolExecutor` (default 4 workers, configurable via `MAX_CONCURRENT_REVIEWS`).
- **Hunk-level sub-splitting**: If a single file diff exceeds the token limit, it is split at `@@` hunk boundaries with the file header preserved.
- **Context minimization**: `git diff -U3` (3 lines of context) is the default, keeping diffs focused.

### Token/Cost Optimization

- **File filtering**: Lock files, minified assets, generated code, and vendor directories are excluded before any API calls.
- **Extension allowlist**: Only known source code extensions are reviewed.
- **Estimated costs**: ~$0.01–0.03 per average PR (200 lines), ~$0.05–0.15 for large PRs (1000+ lines) with GPT-4o.

### Resilience

- **Retry with exponential backoff**: API calls are retried up to `RETRY_MAX_ATTEMPTS` times (default 3) with exponential backoff starting at `RETRY_BASE_DELAY` seconds (default 2.0).

### Future Optimizations (not implemented)

- **Two-tier review**: Use `gpt-4o-mini` for initial triage, only escalate flagged files to `gpt-4o`.
- **Caching**: Skip re-review of unchanged files on `synchronize` events.

## 5. Security

### API Key Management

- `OPENAI_API_KEY` is stored as a GitHub Repository Secret.
- The workflow uses `::add-mask::` to prevent accidental exposure in logs.
- The key is passed via environment variable, never hardcoded or logged.

### Prompt Injection Mitigation

- User-controlled content (the diff) is wrapped in `<code_diff>` XML tags, clearly separating it from system instructions.
- The system prompt includes an explicit directive to ignore instructions within the diff.
- Structured Outputs constrain the model to a fixed JSON schema, limiting the attack surface for output manipulation.

### Fork PR Security

- The workflow uses the `pull_request` event, which **does not** expose repository secrets to fork PRs.
- This means the workflow will fail silently for fork PRs (no API key available). This is the intended, secure behavior.
- **Not used**: `pull_request_target`, which would give fork code access to secrets — a known security vulnerability.
- **Future option**: A `workflow_run`-based two-stage approach could securely support fork PRs by separating code checkout from secret access.

## 6. Assumptions

- An OpenAI API key with GPT-4o access is available.
- The repository has GitHub Actions enabled.
- Review language defaults to English but is configurable via the `REVIEW_LANGUAGE` environment variable (e.g. `tr` for Turkish).
- For production use, error alerting and observability should be added.
- The system is additive — it supports human reviewers, it does not replace them.
