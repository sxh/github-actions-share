#!/usr/bin/env python3
"""
DeepSeek Full-Context PR Review

For each file changed in a PR, reads the COMPLETE file content (not just the diff)
and sends it to DeepSeek for review. Posts the result as a PR review comment.

Usage:
  python3 deepseek-review.py

Environment:
  CHAT_TOKEN     - DeepSeek API token
  GITHUB_TOKEN   - GitHub token for posting the review
  PR_NUMBER      - Pull request number
  REPO           - GitHub repository (owner/name), e.g. "sxh/paints-app"
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOTAL_CHARS = 300_000  # total prompt budget (~75k tokens, room for 128k context)
EXCLUDED_PATTERNS = (
    # Lock / generated files
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Gemfile.lock",
    # Binary / media
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".eot",
    ".ttf",
    ".pdf",
    # Build artifacts
    ".map",
)

SYSTEM_PROMPT = """\
You are a professional code review assistant analyzing a GitHub Pull Request.

For each file changed in this PR, the COMPLETE file content is provided below, \
not just the diff hunks. This means you can see imports, type definitions, \
interfaces, and other declarations that give full context for your review.

Focus on identifying:
- Logic errors and bugs that would cause incorrect behavior at runtime
- Type safety issues (mismatched types, missing null checks, etc.)
- Security vulnerabilities (XSS, injection, credential leaks, etc.)
- Resource leaks (unclosed connections, subscriptions, timers, etc.)
- Incorrect assumptions about data flow or API contracts
- Broken error handling (silently caught exceptions, missing error propagation)
- HTML validity (invalid nesting of interactive elements, malformed tags, etc.)
- Accessibility violations (missing ARIA attributes, broken keyboard navigation, screen reader issues, color contrast, focus management)
- Event handling bugs (missing stopPropagation, double-firing, event delegation issues, unintended default behavior)
- CSS / styling defects (missing disabled-state cursors, incorrect z-index, layout-breaking rules, responsive gaps)
- Remaining inline `style={…}` attributes that should be moved to a CSS module
- Framework-specific anti-patterns (React controlled components bypassing onChange, form submission conflicts, stale closures, hook dependency arrays, test cleanup omissions)
- [HTML/A11y] Labels with both implicit association (wrapping a form control) and explicit association (`htmlFor`/`for` attribute) — the explicit `htmlFor` is redundant when the control is nested inside the `<label>`, and the combination can cause duplicate activation events dispatched to the control in some browsers and testing environments
- [JS/TS] Global event handlers on document/root elements that check specific element types (e.g., `instanceof HTMLInputElement`) instead of using the DOM's general "already-handled" mechanism (`event.defaultPrevented`) — type-checking individual child elements is brittle and misses custom/future components
- [JS/TS] Manual synchronization wrappers in tests (e.g., `act()`, `flushPromises()`, `runAllTimers()`) — these are code smells that mask async warnings; the correct pattern is to wait for a visible outcome of the async operation (e.g., `await screen.findByText(...)`)
- Test file organization issues: multiple test files for one production component (should consolidate into one), filenames embedding fix history or bug numbers, or non-standard naming conventions
- Unstable callback references in setup/teardown patterns (e.g., `useEffect` dependency arrays, `Disposable.using`, `addEventListener`/`removeEventListener` pairs) that cause repeated teardown-and-recreate cycles on every render or update
- Files that are excessively large — suggest ways to split them into smaller, focused modules

Be conservative about flagging:
- Style preferences or formatting (unless they cause actual bugs)
- Code that follows existing project conventions (even if unconventional)

For each issue you identify, cite the specific file and line number.
If you are uncertain about an issue, frame it as a question rather than a defect.\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def git(args: list[str], check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def should_exclude(path: str) -> bool:
    """Return True if the file should be skipped."""
    for pat in EXCLUDED_PATTERNS:
        if path.endswith(pat):
            return True
    return False


def build_prompt() -> str:
    """Build the user prompt containing full file contents for all changed files."""

    # Priority 1: use environment variables from the workflow template
    # (these come directly from github.event.pull_request.base.sha / .head.sha)
    base_sha = os.environ.get("BASE_SHA")
    head_sha = os.environ.get("HEAD_SHA")

    if base_sha and head_sha:
        print(f"Using BASE_SHA/HEAD_SHA from environment", file=sys.stderr)

    else:
        # Priority 2: resolve from git commit topology
        parent2 = git(["rev-parse", "--verify", "HEAD^2"], check=False)
        has_two_parents = bool(parent2) and "fatal" not in parent2

        if has_two_parents:
            base_sha = git(["rev-parse", "HEAD^1"])
            head_sha = git(["rev-parse", "HEAD^2"])
        else:
            # Fallback: diff against first parent only
            print(
                "WARNING: merge commit does not have two parents, using HEAD^ as base",
                file=sys.stderr,
            )
            base_sha = git(["rev-parse", "--verify", "HEAD^"], check=False)
            if not base_sha or "fatal" in base_sha:
                print(
                    "ERROR: Cannot determine base SHA (HEAD has no parent and "
                    "BASE_SHA env var is not set). Skipping review.",
                    file=sys.stderr,
                )
                return ""
            head_sha = git(["rev-parse", "HEAD"])

    changed_files_raw = git(["diff", "--name-only", base_sha, head_sha])
    if not changed_files_raw:
        print("No changed files found.", file=sys.stderr)
        return ""

    changed_files = [f for f in changed_files_raw.split("\n") if f]

    # Build the prompt content
    parts: list[str] = []
    total_chars = 0
    skipped_count = 0
    binary_count = 0

    for filepath in changed_files:
        if should_exclude(filepath):
            skipped_count += 1
            continue

        # Try to read the full file content from the PR head
        try:
            content = git(["show", f"{head_sha}:{filepath}"])
        except subprocess.CalledProcessError:
            binary_count += 1
            continue

        block = f"--- File: {filepath}\n```\n{content}\n```"
        block_len = len(block)

        if total_chars + block_len > MAX_TOTAL_CHARS:
            print(f"TRUNCATED: hit {MAX_TOTAL_CHARS} char limit at {filepath}", file=sys.stderr)
            break

        parts.append(block)
        total_chars += block_len

    if not parts:
        return ""

    summary_lines = [
        f"# Pull Request Review Request",
        f"",
        f"Repository: {os.environ.get('REPO', '?')}",
        f"PR: #{os.environ.get('PR_NUMBER', '?')}",
        f"Base: {base_sha[:8]}  Head: {head_sha[:8]}",
        f"",
        f"Changed files: {len(changed_files)}",
        f"Files provided for review: {len(parts)}",
    ]
    if skipped_count:
        summary_lines.append(f"Files skipped (excluded): {skipped_count}")
    if binary_count:
        summary_lines.append(f"Binary files: {binary_count}")

    summary = "\n".join(summary_lines)

    return f"{summary}\n\n---\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def call_deepseek(user_prompt: str) -> str:
    """Send the prompt to DeepSeek and return the response text."""
    payload = json.dumps({
        "model": "deepseek-reasoner",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {os.environ['CHAT_TOKEN']}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"DeepSeek API error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"DeepSeek API network error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    choices = data.get("choices", [])
    if not choices:
        print(f"DeepSeek API returned no choices: {data}", file=sys.stderr)
        sys.exit(1)

    return choices[0]["message"]["content"]


def post_review(body: str):
    """Post the review as a PR review comment on GitHub."""
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]

    payload = json.dumps({
        "body": body,
        "event": "COMMENT",
    }).encode("utf-8")

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/vnd.github.v3+json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"Review posted: {result.get('html_url', url)}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"GitHub API error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Validate environment
    for var in ("CHAT_TOKEN", "GITHUB_TOKEN", "PR_NUMBER", "REPO"):
        if var not in os.environ or not os.environ[var]:
            print(f"Missing required environment variable: {var}", file=sys.stderr)
            sys.exit(1)

    print("Building prompt with full file contents...", file=sys.stderr)
    prompt = build_prompt()

    if not prompt:
        print("Nothing to review (all files excluded or empty diff). Exiting.", file=sys.stderr)
        return

    print(f"Prompt built ({len(prompt)} chars). Calling DeepSeek...", file=sys.stderr)
    review = call_deepseek(prompt)

    formatted = f"## DeepSeek Code Review\n\n{review}"
    print(f"Review generated ({len(formatted)} chars). Posting to PR #{os.environ['PR_NUMBER']}...", file=sys.stderr)
    post_review(formatted)

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
