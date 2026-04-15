# AI-Powered PR Code Review

Automatically review Pull Requests using OpenAI GPT-4o. When a PR is opened or updated, the system analyzes the code diff and posts structured feedback directly on the PR.

## How It Works

1. A Pull Request is opened (or updated) on GitHub
2. GitHub Actions workflow triggers automatically
3. The review script fetches the PR diff
4. The diff is filtered (only source code, no lock files) and chunked to fit token limits
5. Each chunk is sent to OpenAI GPT-4o with a structured review prompt
6. Results are posted as a PR comment with a severity table and detailed suggestions
7. HIGH severity issues also get inline review comments on the specific file/line

## Example Output

When the review completes, a comment like this appears on the PR:

> ## 🤖 AI Code Review
>
> ### Summary
> The changes introduce a new authentication module with several areas for improvement.
>
> **Found 3 issue(s):** 🔴 1 High | 🟡 1 Medium | 🟢 1 Low
>
> | Severity | File | Line | Category | Issue |
> |----------|------|------|----------|-------|
> | 🔴 HIGH | `src/auth.py` | 42 | security | SQL injection via string concatenation |
> | 🟡 MEDIUM | `src/utils.py` | 15 | performance | List copied unnecessarily in loop |
> | 🟢 LOW | `src/handler.py` | 88 | readability | Variable name `x` is not descriptive |

## Setup

### 1. Add OpenAI API Key

Go to your repository **Settings → Secrets and variables → Actions** and add:

| Secret Name | Value |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (must have GPT-4o access) |

`GITHUB_TOKEN` is automatically provided by GitHub Actions — no setup needed.

### 2. Copy Files to Your Repository

Copy the following files into your repository:

```
.github/
  workflows/
    ai-review.yml
scripts/
  review.py
  prompts/
    system_prompt.txt
requirements.txt
```

### 3. Push and Open a PR

Once the files are in your repository, open any Pull Request. The AI review will run automatically.

## Configuration

All runtime settings can be overridden via **GitHub Actions Variables** (Settings → Variables → Actions) without editing code:

| Variable | Default | Description |
|---|---|---|
| `REVIEW_MODEL` | `gpt-4o` | OpenAI model to use |
| `MAX_TOKENS_PER_CHUNK` | `3000` | Token limit per review chunk |
| `MAX_CONCURRENT_REVIEWS` | `4` | Max parallel API calls for chunks |
| `REVIEW_LANGUAGE` | `en` | Review output language (e.g. `tr`, `de`, `ja`) |
| `RETRY_MAX_ATTEMPTS` | `3` | Max retry attempts on API failure |
| `RETRY_BASE_DELAY` | `2.0` | Base delay (seconds) for exponential backoff |

### Reviewed File Types

By default, the following extensions are reviewed:

`.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.java`, `.go`, `.rs`, `.rb`, `.kt`, `.swift`, `.c`, `.cpp`, `.h`, `.hpp`, `.cs`

To modify, edit the `REVIEW_EXTENSIONS` set in `scripts/review.py`.

### Excluded Patterns

Lock files, minified assets, generated code, and vendor directories are excluded. See `EXCLUDED_PATTERNS` in `scripts/review.py`.

### Prompt Customization

Edit `scripts/prompts/system_prompt.txt` to:
- Add your team's coding standards
- Adjust severity definitions
- Add or remove review categories

## Project Structure

```
.github/workflows/ai-review.yml   # GitHub Actions workflow
scripts/
  review.py                        # Main review script
  prompts/
    system_prompt.txt              # System prompt for OpenAI
tests/
  test_review.py                   # Unit tests (37 tests)
requirements.txt                   # Python dependencies
docs/
  ARCHITECTURE.md                  # Architecture documentation
README.md                          # This file
.gitignore                         # Git ignore rules
```

## Running Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

## Cost Estimates

| PR Size | Estimated Cost |
|---|---|
| Small (< 200 lines) | $0.01 – $0.03 |
| Medium (200–500 lines) | $0.03 – $0.07 |
| Large (1000+ lines) | $0.05 – $0.15 |

## Security Notes

- **API keys** are stored as GitHub Secrets and masked in logs.
- **Fork PRs** cannot access secrets, so the review will not run for fork PRs (by design).
- **Prompt injection** is mitigated by separating user content from system instructions and using structured output constraints.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full security analysis.

## Limitations

- Does not replace human review — designed to assist, not replace.
- Token limits mean very large files may be split across multiple API calls.
- Inline comment line mapping may occasionally be imprecise for complex diffs.
- Fork PRs are not reviewed (security trade-off).
