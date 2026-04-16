"""
AI-Powered PR Code Review Script

Fetches the PR diff from GitHub, sends it to OpenAI for structured review,
and posts the results as PR comments.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tiktoken
from github import Auth, Github
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# ---------------------------------------------------------------------------

MAX_TOKENS_PER_CHUNK = int(os.environ.get("MAX_TOKENS_PER_CHUNK", "3000"))
MODEL = os.environ.get("REVIEW_MODEL", "gpt-4o")
MAX_CONCURRENT_REVIEWS = int(os.environ.get("MAX_CONCURRENT_REVIEWS", "4"))
REVIEW_LANGUAGE = os.environ.get("REVIEW_LANGUAGE", "en")
RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "2.0"))

REVIEW_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".rb", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp", ".cs",
}
EXCLUDED_PATTERNS = [
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"pnpm-lock\.yaml$",
    r"\.min\.(js|css)$",
    r"__generated__",
    r"\.g\.dart$",
    r"\.snap$",
    r"dist/",
    r"build/",
    r"vendor/",
]

REVIEW_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "readability",
                                    "performance",
                                    "security",
                                    "best_practice",
                                    "bug_risk",
                                ],
                            },
                            "issue": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": [
                            "file",
                            "line",
                            "severity",
                            "category",
                            "issue",
                            "suggestion",
                        ],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["reviews", "summary"],
            "additionalProperties": False,
        },
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"Error: {name} environment variable is not set.")
        sys.exit(1)
    return value


def load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "prompts" / "system_prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    if REVIEW_LANGUAGE != "en":
        prompt += (
            f"\n\nIMPORTANT: Write ALL your review output (summary, issue "
            f"descriptions, and suggestions) in {REVIEW_LANGUAGE}."
        )
    return prompt


def count_tokens(text: str, model: str = MODEL) -> int:
    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def retry_with_backoff(func, *args, **kwargs):
    """Call *func* with exponential backoff on transient failures."""
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt == RETRY_MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"  Attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed: {exc}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Diff processing
# ---------------------------------------------------------------------------


def should_review_file(filepath: str) -> bool:
    """Return True if the file should be included in the review."""
    ext = Path(filepath).suffix.lower()
    if ext not in REVIEW_EXTENSIONS:
        return False
    for pattern in EXCLUDED_PATTERNS:
        if re.search(pattern, filepath):
            return False
    return True


def parse_diff(raw_diff: str) -> dict[str, str]:
    """
    Parse a unified diff into a mapping of filepath -> diff content.
    Only includes files that pass the review filter.
    """
    files: dict[str, str] = {}
    current_file: str | None = None
    current_lines: list[str] = []

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_file and should_review_file(current_file):
                files[current_file] = "".join(current_lines)
            match = re.search(r" b/(.+)$", line)
            current_file = match.group(1) if match else None
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_file and should_review_file(current_file):
        files[current_file] = "".join(current_lines)

    return files


def chunk_diff(file_diffs: dict[str, str]) -> list[dict[str, str]]:
    """
    Group file diffs into chunks that fit within the token limit.
    Large single-file diffs are split by hunk boundaries.
    """
    chunks: list[dict[str, str]] = []
    current_chunk: dict[str, str] = {}
    current_tokens = 0

    for filepath, diff_content in file_diffs.items():
        file_tokens = count_tokens(diff_content)

        if file_tokens > MAX_TOKENS_PER_CHUNK:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = {}
                current_tokens = 0
            for sub_chunk in _split_large_diff(filepath, diff_content):
                chunks.append(sub_chunk)
            continue

        if current_tokens + file_tokens > MAX_TOKENS_PER_CHUNK:
            chunks.append(current_chunk)
            current_chunk = {}
            current_tokens = 0

        current_chunk[filepath] = diff_content
        current_tokens += file_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _split_large_diff(filepath: str, diff_content: str) -> list[dict[str, str]]:
    """Split a large single-file diff by hunk boundaries (@@ markers)."""
    header_lines: list[str] = []
    hunks: list[list[str]] = []
    current_hunk: list[str] = []

    for line in diff_content.splitlines(keepends=True):
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = [line]
        elif not hunks and not current_hunk:
            header_lines.append(line)
        else:
            current_hunk.append(line)

    if current_hunk:
        hunks.append(current_hunk)

    header = "".join(header_lines)
    sub_chunks: list[dict[str, str]] = []
    accumulated: list[str] = []
    accumulated_tokens = count_tokens(header)

    for hunk in hunks:
        hunk_text = "".join(hunk)
        hunk_tokens = count_tokens(hunk_text)

        if accumulated_tokens + hunk_tokens > MAX_TOKENS_PER_CHUNK and accumulated:
            sub_chunks.append({filepath: header + "".join(accumulated)})
            accumulated = []
            accumulated_tokens = count_tokens(header)

        accumulated.append(hunk_text)
        accumulated_tokens += hunk_tokens

    if accumulated:
        sub_chunks.append({filepath: header + "".join(accumulated)})

    return sub_chunks


# ---------------------------------------------------------------------------
# GitHub interaction
# ---------------------------------------------------------------------------


def get_pr_diff(github_token: str, repo_name: str, pr_number: int) -> str:
    """Fetch the raw unified diff for a pull request via PyGithub."""
    gh = Github(auth=Auth.Token(github_token))
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    comparison = repo.compare(pr.base.sha, pr.head.sha)

    diff_parts: list[str] = []
    for file in comparison.files:
        if file.patch:
            diff_parts.append(
                f"diff --git a/{file.filename} b/{file.filename}\n{file.patch}"
            )

    return "\n".join(diff_parts)


def post_review_comment(
    github_token: str,
    repo_name: str,
    pr_number: int,
    body: str,
) -> None:
    """Post a general comment on the PR."""
    gh = Github(auth=Auth.Token(github_token))
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(body)


def post_inline_comments(
    github_token: str,
    repo_name: str,
    pr_number: int,
    head_sha: str,
    reviews: list[dict],
) -> None:
    """Post inline review comments for HIGH severity issues."""
    high_reviews = [r for r in reviews if r["severity"] == "high"]
    if not high_reviews:
        return

    gh = Github(auth=Auth.Token(github_token))
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    comments = []
    for review in high_reviews:
        line = review["line"]
        if line < 1:
            line = 1
        comments.append(
            {
                "path": review["file"],
                "line": line,
                "body": (
                    f"**{review['severity'].upper()}** - {review['category']}\n\n"
                    f"{review['issue']}\n\n"
                    f"**Suggestion:** {review['suggestion']}"
                ),
            }
        )

    if comments:
        try:
            pr.create_review(
                commit=repo.get_commit(head_sha),
                body="AI Review: High severity issues found inline.",
                event="COMMENT",
                comments=comments,
            )
        except Exception as exc:
            print(f"Warning: Could not post inline comments: {exc}")
            print("Falling back to general comment only.")


# ---------------------------------------------------------------------------
# AI review
# ---------------------------------------------------------------------------

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _call_openai(client: OpenAI, system_prompt: str, user_message: str) -> dict:
    """Single OpenAI API call (used as the retry target)."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format=REVIEW_SCHEMA,
        temperature=0.2,
    )
    return json.loads(response.choices[0].message.content)


def review_chunk(client: OpenAI, system_prompt: str, chunk: dict[str, str]) -> dict:
    """Send a diff chunk to OpenAI and return the structured review."""
    diff_text = "\n".join(
        f"=== {filepath} ===\n{content}" for filepath, content in chunk.items()
    )

    user_message = f"<code_diff>\n{diff_text}\n</code_diff>"

    return retry_with_backoff(_call_openai, client, system_prompt, user_message)


def merge_reviews(results: list[dict]) -> dict:
    """Merge multiple chunk review results into a single result."""
    all_reviews: list[dict] = []
    summaries: list[str] = []

    for result in results:
        all_reviews.extend(result.get("reviews", []))
        if result.get("summary"):
            summaries.append(result["summary"])

    all_reviews.sort(
        key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(r["severity"], 3)
    )

    combined_summary = " ".join(summaries) if summaries else "No issues found."

    return {"reviews": all_reviews, "summary": combined_summary}


def format_review_comment(result: dict) -> str:
    """Format the review result into a Markdown comment for the PR."""
    reviews = result["reviews"]
    summary = result["summary"]

    lines = [
        "## 🤖 AI Code Review\n",
        f"### Summary\n{summary}\n",
    ]

    if not reviews:
        lines.append("✅ No issues found. The code looks good!\n")
        return "\n".join(lines)

    high_count = sum(1 for r in reviews if r["severity"] == "high")
    medium_count = sum(1 for r in reviews if r["severity"] == "medium")
    low_count = sum(1 for r in reviews if r["severity"] == "low")

    lines.append(
        f"**Found {len(reviews)} issue(s):** "
        f"🔴 {high_count} High | 🟡 {medium_count} Medium | 🟢 {low_count} Low\n"
    )

    lines.append("### Findings\n")
    lines.append("| Severity | File | Line | Category | Issue |")
    lines.append("|----------|------|------|----------|-------|")

    for r in reviews:
        emoji = SEVERITY_EMOJI.get(r["severity"], "⚪")
        severity = f"{emoji} {r['severity'].upper()}"
        lines.append(
            f"| {severity} | `{r['file']}` | {r['line']} "
            f"| {r['category']} | {r['issue']} |"
        )

    lines.append("\n### Detailed Suggestions\n")

    for r in reviews:
        emoji = SEVERITY_EMOJI.get(r["severity"], "⚪")
        lines.append(
            f"**{emoji} {r['severity'].upper()} — "
            f"`{r['file']}:{r['line']}` ({r['category']})**"
        )
        lines.append(f"> {r['issue']}")
        lines.append(f">\n> **Suggestion:** {r['suggestion']}\n")

    lines.append("---")
    lines.append(
        "*This review was generated automatically by AI. "
        "Please verify suggestions before applying.*"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _review_chunks_parallel(
    client: OpenAI, system_prompt: str, chunks: list[dict[str, str]]
) -> list[dict]:
    """Review chunks concurrently using a thread pool."""
    results: list[dict | None] = [None] * len(chunks)

    def _worker(index: int, chunk: dict[str, str]) -> tuple[int, dict]:
        print(f"  Reviewing chunk {index + 1}/{len(chunks)}...")
        return index, review_chunk(client, system_prompt, chunk)

    workers = min(MAX_CONCURRENT_REVIEWS, len(chunks))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_worker, i, c): i for i, c in enumerate(chunks)
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    return results  # type: ignore[return-value]


def main() -> None:
    github_token = get_env("GITHUB_TOKEN")
    openai_api_key = get_env("OPENAI_API_KEY")
    repo_name = get_env("REPO_FULL_NAME")
    pr_number = int(get_env("PR_NUMBER"))
    head_sha = get_env("HEAD_SHA")

    print(f"Reviewing PR #{pr_number} in {repo_name}")
    print(f"Config: model={MODEL}, chunk_tokens={MAX_TOKENS_PER_CHUNK}, "
          f"concurrency={MAX_CONCURRENT_REVIEWS}, language={REVIEW_LANGUAGE}")

    raw_diff = get_pr_diff(github_token, repo_name, pr_number)

    if not raw_diff.strip():
        print("No diff content found. Skipping review.")
        return

    file_diffs = parse_diff(raw_diff)
    print(f"Found {len(file_diffs)} reviewable file(s)")

    if not file_diffs:
        print("No reviewable files in this PR. Skipping.")
        return

    chunks = chunk_diff(file_diffs)
    print(f"Split into {len(chunks)} chunk(s) for review")

    client = OpenAI(api_key=openai_api_key)
    system_prompt = load_system_prompt()

    if len(chunks) == 1:
        print("Reviewing single chunk...")
        results = [review_chunk(client, system_prompt, chunks[0])]
    else:
        print(f"Reviewing {len(chunks)} chunks in parallel "
              f"(max {min(MAX_CONCURRENT_REVIEWS, len(chunks))} workers)...")
        results = _review_chunks_parallel(client, system_prompt, chunks)

    merged = merge_reviews(results)
    comment_body = format_review_comment(merged)

    print("Posting review comment...")
    post_review_comment(github_token, repo_name, pr_number, comment_body)

    post_inline_comments(
        github_token, repo_name, pr_number, head_sha, merged["reviews"]
    )

    high_count = sum(1 for r in merged["reviews"] if r["severity"] == "high")
    print(
        f"Review complete: {len(merged['reviews'])} issue(s) found "
        f"({high_count} high severity)"
    )


if __name__ == "__main__":
    main()
