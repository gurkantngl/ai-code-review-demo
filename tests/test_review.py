"""Unit tests for the core review logic (no API calls needed)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from scripts.review import (
    chunk_diff,
    count_tokens,
    format_review_comment,
    merge_reviews,
    parse_diff,
    retry_with_backoff,
    should_review_file,
)

# ---------------------------------------------------------------------------
# should_review_file
# ---------------------------------------------------------------------------


class TestShouldReviewFile:
    def test_python_file(self):
        assert should_review_file("src/main.py") is True

    def test_typescript_file(self):
        assert should_review_file("app/index.tsx") is True

    def test_java_file(self):
        assert should_review_file("com/example/App.java") is True

    def test_markdown_rejected(self):
        assert should_review_file("README.md") is False

    def test_json_rejected(self):
        assert should_review_file("config.json") is False

    def test_lock_file_rejected(self):
        assert should_review_file("package-lock.json") is False

    def test_yarn_lock_rejected(self):
        assert should_review_file("yarn.lock") is False

    def test_minified_js_rejected(self):
        assert should_review_file("bundle.min.js") is False

    def test_vendor_dir_rejected(self):
        assert should_review_file("vendor/lib/helper.go") is False

    def test_dist_dir_rejected(self):
        assert should_review_file("dist/app.js") is False

    def test_generated_file_rejected(self):
        assert should_review_file("src/__generated__/types.ts") is False

    def test_snap_rejected(self):
        assert should_review_file("Component.test.tsx.snap") is False

    def test_case_insensitive_extension(self):
        assert should_review_file("Main.PY") is True


# ---------------------------------------------------------------------------
# parse_diff
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index abc..def 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
+import os
 import sys
 
 def main():
diff --git a/README.md b/README.md
index 111..222 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # Title
+Some description
diff --git a/src/utils.ts b/src/utils.ts
index 333..444 100644
--- a/src/utils.ts
+++ b/src/utils.ts
@@ -5,2 +5,3 @@
 export function add(a: number, b: number) {
+  console.log(a, b);
   return a + b;
"""


class TestParseDiff:
    def test_parses_reviewable_files(self):
        result = parse_diff(SAMPLE_DIFF)
        assert "src/app.py" in result
        assert "src/utils.ts" in result

    def test_excludes_non_code_files(self):
        result = parse_diff(SAMPLE_DIFF)
        assert "README.md" not in result

    def test_file_count(self):
        result = parse_diff(SAMPLE_DIFF)
        assert len(result) == 2

    def test_diff_content_preserved(self):
        result = parse_diff(SAMPLE_DIFF)
        assert "+import os" in result["src/app.py"]

    def test_empty_diff(self):
        assert parse_diff("") == {}

    def test_only_non_code_files(self):
        diff = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
+hello
"""
        assert parse_diff(diff) == {}


# ---------------------------------------------------------------------------
# chunk_diff
# ---------------------------------------------------------------------------


class TestChunkDiff:
    def test_single_small_file_one_chunk(self):
        diffs = {"a.py": "small content"}
        chunks = chunk_diff(diffs)
        assert len(chunks) == 1
        assert "a.py" in chunks[0]

    def test_multiple_small_files_grouped(self):
        diffs = {
            "a.py": "x " * 10,
            "b.py": "y " * 10,
        }
        chunks = chunk_diff(diffs)
        assert len(chunks) == 1

    def test_empty_input(self):
        assert chunk_diff({}) == []


# ---------------------------------------------------------------------------
# merge_reviews
# ---------------------------------------------------------------------------

SAMPLE_REVIEWS = [
    {
        "reviews": [
            {
                "file": "a.py",
                "line": 10,
                "severity": "low",
                "category": "readability",
                "issue": "Bad name",
                "suggestion": "Rename it",
            },
            {
                "file": "a.py",
                "line": 20,
                "severity": "high",
                "category": "security",
                "issue": "SQL injection",
                "suggestion": "Use parameterized queries",
            },
        ],
        "summary": "Chunk 1 summary.",
    },
    {
        "reviews": [
            {
                "file": "b.ts",
                "line": 5,
                "severity": "medium",
                "category": "performance",
                "issue": "N+1 query",
                "suggestion": "Batch queries",
            },
        ],
        "summary": "Chunk 2 summary.",
    },
]


class TestMergeReviews:
    def test_combines_all_reviews(self):
        merged = merge_reviews(SAMPLE_REVIEWS)
        assert len(merged["reviews"]) == 3

    def test_sorted_by_severity(self):
        merged = merge_reviews(SAMPLE_REVIEWS)
        severities = [r["severity"] for r in merged["reviews"]]
        assert severities == ["high", "medium", "low"]

    def test_combines_summaries(self):
        merged = merge_reviews(SAMPLE_REVIEWS)
        assert "Chunk 1 summary." in merged["summary"]
        assert "Chunk 2 summary." in merged["summary"]

    def test_empty_input(self):
        merged = merge_reviews([])
        assert merged["reviews"] == []
        assert merged["summary"] == "No issues found."

    def test_empty_reviews_in_chunks(self):
        merged = merge_reviews([{"reviews": [], "summary": "All good."}])
        assert merged["reviews"] == []
        assert merged["summary"] == "All good."


# ---------------------------------------------------------------------------
# format_review_comment
# ---------------------------------------------------------------------------


class TestFormatReviewComment:
    def test_no_issues_message(self):
        result = format_review_comment({"reviews": [], "summary": "Looks good."})
        assert "No issues found" in result
        assert "Looks good." in result

    def test_issues_table_present(self):
        data = {
            "reviews": [
                {
                    "file": "x.py",
                    "line": 1,
                    "severity": "high",
                    "category": "security",
                    "issue": "Problem",
                    "suggestion": "Fix it",
                },
            ],
            "summary": "One issue.",
        }
        result = format_review_comment(data)
        assert "| Severity |" in result
        assert "`x.py`" in result
        assert "HIGH" in result
        assert "Fix it" in result

    def test_severity_counts(self):
        data = {
            "reviews": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "high",
                    "category": "security",
                    "issue": "A",
                    "suggestion": "B",
                },
                {
                    "file": "b.py",
                    "line": 2,
                    "severity": "low",
                    "category": "readability",
                    "issue": "C",
                    "suggestion": "D",
                },
            ],
            "summary": "Two issues.",
        }
        result = format_review_comment(data)
        assert "1 High" in result
        assert "1 Low" in result
        assert "0 Medium" in result

    def test_disclaimer_present(self):
        data = {
            "reviews": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "low",
                    "category": "readability",
                    "issue": "X",
                    "suggestion": "Y",
                },
            ],
            "summary": "Has issues.",
        }
        result = format_review_comment(data)
        assert "generated automatically by AI" in result


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------


class TestRetryWithBackoff:
    def test_succeeds_first_try(self):
        assert retry_with_backoff(lambda: 42) == 42

    @patch("scripts.review.time.sleep")
    def test_retries_on_failure(self, mock_sleep):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient")
            return "ok"

        result = retry_with_backoff(flaky)
        assert result == "ok"
        assert call_count == 3
        assert mock_sleep.call_count == 2

    @patch("scripts.review.time.sleep")
    def test_raises_after_max_attempts(self, mock_sleep):
        def always_fail():
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            retry_with_backoff(always_fail)


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_nonempty_string(self):
        tokens = count_tokens("Hello, world!")
        assert tokens > 0

    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_returns_int(self):
        assert isinstance(count_tokens("test"), int)
