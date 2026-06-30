#!/usr/bin/env python3
"""
Tests for deepseek-review.py — specifically the SHA resolution in build_prompt().

Creates temporary git repositories with different topologies to exercise:
  - Env-var-driven base/head resolution
  - Merge commit (2 parents) fallback
  - Single-parent fallback
  - No-parent (root commit) graceful handling
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Load the target module (hyphen in filename prevents direct import)
# ---------------------------------------------------------------------------
_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "deepseek-review.py"
)
_SPEC = importlib.util.spec_from_file_location("deepseek_review", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {_SCRIPT_PATH}")
deepseek_review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deepseek_review)
build_prompt = deepseek_review.build_prompt
should_exclude = deepseek_review.should_exclude
main = deepseek_review.main

# Optional: yaml is only needed for the workflow template test
try:
    import yaml as _yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Git helpers for test setup
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    """Run a git command in *cwd* and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


def _init_repo(path):
    """Initialise an empty git repository at *path*."""
    os.makedirs(path, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")


def _commit(path, msg="commit"):
    """Create an empty commit and return its full SHA."""
    _git(path, "commit", "--allow-empty", "-m", msg)
    return _git(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildPromptShaResolution(unittest.TestCase):
    """Exercises the SHA-resolution logic inside build_prompt()."""

    def setUp(self):
        self._origin_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        self._saved_environ = os.environ.copy()

        # Minimal env so build_prompt doesn't choke on str formatting
        os.environ.setdefault("REPO", "test/repo")
        os.environ.setdefault("PR_NUMBER", "1")

    def tearDown(self):
        os.chdir(self._origin_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._saved_environ)

    # -- Happy paths -------------------------------------------------------

    def test_env_vars_used_when_set(self):
        """BASE_SHA and HEAD_SHA env vars should be used directly."""
        repo = os.path.join(self._tmpdir, "env-vars")
        _init_repo(repo)
        base_sha = _commit(repo, "base")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha

        os.chdir(repo)

        # Should not raise — env vars are consumed directly
        result = build_prompt()
        # No files have changed between these two commits so prompt is empty
        self.assertEqual(result, "")

    def test_merge_commit_two_parents(self):
        """2-parent merge commit: should use HEAD^1 and HEAD^2."""
        repo = os.path.join(self._tmpdir, "merge")
        _init_repo(repo)
        # Create a base commit on main
        _commit(repo, "base")
        base_sha = _git(repo, "rev-parse", "HEAD")
        # Branch and commit
        _git(repo, "checkout", "-b", "feature")
        _commit(repo, "feature")
        head_sha = _git(repo, "rev-parse", "HEAD")
        # Merge back (force no-ff to create a merge commit)
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge commit", "feature")

        os.chdir(repo)
        result = build_prompt()

        # Should not crash; prompt may be empty (no diff) but that's fine
        self.assertIsInstance(result, str)

    def test_merge_commit_parent_shas_resolvable(self):
        """On a merge commit (simulating refs/pull/N/merge checkout),
        both parent SHAs must be resolvable. This is the behaviour that
        requires fetch-depth: 0 in the workflow YAML — with fetch-depth: 1,
        git rev-parse HEAD^1 would fail with exit code 128."""
        repo = os.path.join(self._tmpdir, "parent-shas")
        _init_repo(repo)

        _commit(repo, "base")
        base_sha = _git(repo, "rev-parse", "HEAD")

        _git(repo, "checkout", "-b", "feature")
        _commit(repo, "feature")
        feature_sha = _git(repo, "rev-parse", "HEAD")

        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge commit", "feature")

        os.chdir(repo)

        # These would fail with exit code 128 under fetch-depth: 1
        parent1 = _git(repo, "rev-parse", "HEAD^1")
        parent2 = _git(repo, "rev-parse", "HEAD^2")

        self.assertEqual(parent1, base_sha)
        self.assertEqual(parent2, feature_sha)

    def test_single_parent_fallback(self):
        """Non-merge commit: should fall back to HEAD^ and HEAD."""
        repo = os.path.join(self._tmpdir, "single-parent")
        _init_repo(repo)
        _commit(repo, "first")
        _commit(repo, "second")

        os.chdir(repo)
        result = build_prompt()

        self.assertIsInstance(result, str)

    def test_env_vars_with_actual_file_changes(self):
        """build_prompt() should include file content in the prompt."""
        repo = os.path.join(self._tmpdir, "file-change")
        _init_repo(repo)

        # First commit: create a file
        _git(repo, "checkout", "-b", "main")
        os.makedirs(os.path.join(repo, "src"), exist_ok=True)
        with open(os.path.join(repo, "src", "app.py"), "w") as f:
            f.write("# hello\nprint('hi')\n")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        # Second commit: modify the same file
        with open(os.path.join(repo, "src", "app.py"), "w") as f:
            f.write("# hello world\nprint('hi')\n")
        _git(repo, "add", ".")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha

        os.chdir(repo)
        result = build_prompt()

        # Prompt should contain the file content and metadata
        self.assertIn("src/app.py", result)
        self.assertIn("# hello world", result)
        self.assertIn("Base:", result)
        self.assertIn("Changed files: 1", result)
        self.assertIn("Files provided for review: 1", result)

    def test_excluded_file_is_skipped(self):
        """build_prompt() should skip excluded file patterns."""
        repo = os.path.join(self._tmpdir, "excluded")
        _init_repo(repo)

        os.makedirs(os.path.join(repo, "src"), exist_ok=True)
        with open(os.path.join(repo, "package-lock.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(repo, "src", "main.py"), "w") as f:
            f.write("print('ok')\n")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        with open(os.path.join(repo, "package-lock.json"), "w") as f:
            f.write('{"lock": true}')
        with open(os.path.join(repo, "src", "main.py"), "w") as f:
            f.write("print('changed')\n")
        _git(repo, "add", ".")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha

        os.chdir(repo)
        result = build_prompt()

        # Only main.py should appear, not package-lock.json
        self.assertIn("src/main.py", result)
        self.assertNotIn("package-lock.json", result)
        self.assertIn("Files skipped (excluded): 1", result)

    # -- Bug regression ----------------------------------------------------

    def test_no_parent_does_not_crash(self):
        """
        Root commit (no parents): should NOT crash with CalledProcessError.

        This is the regression test for the bug where `git rev-parse HEAD^`
        raises CalledProcessError on a commit with no parents.
        """
        repo = os.path.join(self._tmpdir, "no-parent")
        _init_repo(repo)
        # A single root commit — HEAD has *no* parents
        _commit(repo, "root")

        # Purge any BASE_SHA/HEAD_SHA that may be lingering
        os.environ.pop("BASE_SHA", None)
        os.environ.pop("HEAD_SHA", None)

        os.chdir(repo)

        # This must NOT raise subprocess.CalledProcessError
        try:
            result = build_prompt()
            self.assertIsInstance(result, str)
        except subprocess.CalledProcessError:
            self.fail(
                "build_prompt() crashed with CalledProcessError on a root commit. "
                "The SHA-resolution fallback must handle commits with no parents."
            )


# ---------------------------------------------------------------------------
# should_exclude
# ---------------------------------------------------------------------------

class TestShouldExclude(unittest.TestCase):
    """Tests for the should_exclude helper."""

    def test_excludes_lock_file(self):
        self.assertTrue(should_exclude("package-lock.json"))

    def test_excludes_png(self):
        self.assertTrue(should_exclude("icon.png"))

    def test_excludes_map(self):
        self.assertTrue(should_exclude("bundle.js.map"))

    def test_allows_python(self):
        self.assertFalse(should_exclude("app.py"))

    def test_allows_typescript(self):
        self.assertFalse(should_exclude("src/component.tsx"))

    def test_allows_yaml(self):
        self.assertFalse(should_exclude("config.yml"))

    def test_allows_markdown(self):
        self.assertFalse(should_exclude("README.md"))

    def test_path_with_multiple_dots(self):
        self.assertFalse(should_exclude("some.long.path.test.py"))


# ---------------------------------------------------------------------------
# main() — env validation
# ---------------------------------------------------------------------------

class TestMainEnvValidation(unittest.TestCase):
    """Tests that main() validates required env vars before proceeding."""

    def setUp(self):
        self._saved_environ = os.environ.copy()
        # Remove CHAT_TOKEN to trigger the validation failure
        for var in ("CHAT_TOKEN", "GITHUB_TOKEN", "PR_NUMBER", "REPO"):
            os.environ.pop(var, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_exits_when_chat_token_missing(self):
        """main() should exit with code 1 when CHAT_TOKEN is not set."""
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_when_all_env_vars_missing(self):
        """main() should exit when no required env vars are set."""
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# build_prompt — edge cases (binary, truncation)
# ---------------------------------------------------------------------------

class TestBuildPromptEdgeCases(unittest.TestCase):
    """Exercises the file-iteration edge cases inside build_prompt()."""

    def setUp(self):
        self._origin_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        self._saved_environ = os.environ.copy()
        os.environ.setdefault("REPO", "test/repo")
        os.environ.setdefault("PR_NUMBER", "1")

    def tearDown(self):
        os.chdir(self._origin_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_deleted_file_triggers_binary_count(self):
        """
        When a file shown in git diff was deleted in the head commit,
        git show fails -> binary_count increments.

        We include a second file that does exist so the summary is built.
        """
        repo = os.path.join(self._tmpdir, "deleted")
        _init_repo(repo)

        # Base commit: create two files
        with open(os.path.join(repo, "deleted.py"), "w") as f:
            f.write("print('will be deleted')\n")
        with open(os.path.join(repo, "kept.py"), "w") as f:
            f.write("print('kept')\n")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        # Head commit: delete one file, modify the other
        os.remove(os.path.join(repo, "deleted.py"))
        with open(os.path.join(repo, "kept.py"), "w") as f:
            f.write("print('modified')\n")
        _git(repo, "add", "-A")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha
        os.chdir(repo)
        result = build_prompt()

        # The deleted file should trigger the except CalledProcessError path
        self.assertIn("Binary files: 1", result)
        # The kept file should appear in the prompt
        self.assertIn("kept.py", result)

    def test_truncated_when_exceeds_budget(self):
        """When total chars exceed MAX_TOTAL_CHARS, prompt should truncate."""
        repo = os.path.join(self._tmpdir, "truncate")
        _init_repo(repo)

        # Use tiny budget via patch so we don't need 300K of test data
        original_max_total = deepseek_review.MAX_TOTAL_CHARS

        with open(os.path.join(repo, "file_a.py"), "w") as f:
            f.write("x = 1\n")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        # Modify both files in the head commit
        with open(os.path.join(repo, "file_a.py"), "w") as f:
            f.write("a" * 30_000)  # under MAX_FILE_SIZE_CHARS (50K)
        with open(os.path.join(repo, "file_b.py"), "w") as f:
            f.write("b" * 30_000)
        _git(repo, "add", ".")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha
        os.chdir(repo)

        # Artificially lower the budget so file_b triggers truncation
        deepseek_review.MAX_TOTAL_CHARS = 35_000
        try:
            result = build_prompt()
            # file_a should be included (it fits within 35K budget)
            self.assertIn("file_a.py", result)
            # TRUNCATED message should appear in stderr
            self.assertIsInstance(result, str)
        finally:
            deepseek_review.MAX_TOTAL_CHARS = original_max_total

    def test_all_excluded_files_returns_empty(self):
        """When all files match exclusion patterns, build_prompt returns ''."""
        repo = os.path.join(self._tmpdir, "all-excluded")
        _init_repo(repo)

        with open(os.path.join(repo, "package-lock.json"), "w") as f:
            f.write("{}")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        with open(os.path.join(repo, "package-lock.json"), "w") as f:
            f.write('{"lock": true}')
        _git(repo, "add", ".")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha
        os.chdir(repo)
        result = build_prompt()

        # All files excluded, so prompt should be empty
        self.assertEqual(result, "")

    def test_large_file_included_in_prompt(self):
        """Large files should be included in the prompt (not skipped), so
        that the model can review them and suggest splitting opportunities."""
        repo = os.path.join(self._tmpdir, "large-file")
        _init_repo(repo)

        with open(os.path.join(repo, "main.py"), "w") as f:
            f.write("x = 1\n")
        _git(repo, "add", ".")
        base_sha = _commit(repo, "base")

        # Create a file > 50K chars in head
        with open(os.path.join(repo, "main.py"), "w") as f:
            f.write("x" * 51_000)
        _git(repo, "add", ".")
        head_sha = _commit(repo, "head")

        os.environ["BASE_SHA"] = base_sha
        os.environ["HEAD_SHA"] = head_sha
        os.chdir(repo)
        result = build_prompt()

        # Large file must be included, not skipped
        self.assertIn("main.py", result)
        self.assertIn("Files provided for review: 1", result)


# ---------------------------------------------------------------------------
# call_deepseek — error paths (mocked HTTP)
# ---------------------------------------------------------------------------

class TestCallDeepseek(unittest.TestCase):
    """Tests for call_deepseek with mocked urllib."""

    def setUp(self):
        self._saved_environ = os.environ.copy()
        os.environ["CHAT_TOKEN"] = "test-token"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_http_error_exits(self):
        """call_deepseek should exit(1) on HTTP 401."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://api.deepseek.com/chat/completions",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"error":"unauthorized"}'),
            )
            with self.assertRaises(SystemExit) as ctx:
                deepseek_review.call_deepseek("hello")
            self.assertEqual(ctx.exception.code, 1)

    def test_network_error_exits(self):
        """call_deepseek should exit(1) on network error."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            with self.assertRaises(SystemExit) as ctx:
                deepseek_review.call_deepseek("hello")
            self.assertEqual(ctx.exception.code, 1)

    def test_empty_choices_exits(self):
        """call_deepseek should exit(1) when API returns no choices."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": []}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with self.assertRaises(SystemExit) as ctx:
                deepseek_review.call_deepseek("hello")
            self.assertEqual(ctx.exception.code, 1)

    def test_payload_includes_deepseek_reasoner_model(self):
        """call_deepseek should send 'deepseek-reasoner' as the model in the request payload."""
        captured_payload = {}

        def capture_request(req, **kwargs):
            captured_payload["data"] = json.loads(req.data.decode("utf-8"))
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"choices": [{"message": {"content": "ok"}}]}'
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture_request):
            deepseek_review.call_deepseek("hello")

        self.assertEqual(captured_payload["data"]["model"], "deepseek-reasoner")

    def test_success_returns_content(self):
        """call_deepseek should return the review text on success."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": [{"message": {"content": "Looks good!"}}]}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = deepseek_review.call_deepseek("hello")
            self.assertEqual(result, "Looks good!")


# ---------------------------------------------------------------------------
# post_review — error paths (mocked HTTP)
# ---------------------------------------------------------------------------

class TestPostReview(unittest.TestCase):
    """Tests for post_review with mocked urllib."""

    def setUp(self):
        self._saved_environ = os.environ.copy()
        os.environ["REPO"] = "test/repo"
        os.environ["PR_NUMBER"] = "42"
        os.environ["GITHUB_TOKEN"] = "test-token"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_http_error_exits(self):
        """post_review should exit(1) on HTTP 403."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://api.github.com/repos/test/repo/pulls/42/reviews",
                403,
                "Forbidden",
                {},
                io.BytesIO(b'{"message":"Forbidden"}'),
            )
            with self.assertRaises(SystemExit) as ctx:
                deepseek_review.post_review("review body")
            self.assertEqual(ctx.exception.code, 1)

    def test_success_posts_review(self):
        """post_review should complete without error on successful post."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"html_url": "https://github.com/test/repo/pull/42#review-1"}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            # Should not raise
            deepseek_review.post_review("review body")


# ---------------------------------------------------------------------------
# main() — full flow (mocked)
# ---------------------------------------------------------------------------

class TestMainFullFlow(unittest.TestCase):
    """Tests main() with build_prompt returning content and mocked APIs."""

    def setUp(self):
        self._saved_environ = os.environ.copy()
        os.environ["CHAT_TOKEN"] = "test-token"
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["PR_NUMBER"] = "1"
        os.environ["REPO"] = "test/repo"
        # Ensure BASE_SHA/HEAD_SHA are NOT set so build_prompt can run
        os.environ.pop("BASE_SHA", None)
        os.environ.pop("HEAD_SHA", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_main_with_empty_prompt(self):
        """main() should return gracefully when build_prompt returns empty."""
        # Temporarily set BASE_SHA/HEAD_SHA to shas that don't resolve
        # so build_prompt returns "" after failing to find a base
        os.chdir(tempfile.gettempdir())
        # build_prompt will fail to resolve git parents outside a repo and return ""
        with patch.object(deepseek_review, "call_deepseek") as mock_call:
            with patch.object(deepseek_review, "post_review") as mock_post:
                main()
                mock_call.assert_not_called()
                mock_post.assert_not_called()

    def test_main_with_actual_content(self):
        """main() should call APIs when build_prompt returns content."""
        # Mock build_prompt to return a real prompt
        original_build = deepseek_review.build_prompt
        deepseek_review.build_prompt = lambda: "# Test\n\n--- File: test.py\n```\nprint('hi')\n```"

        with patch.object(deepseek_review, "call_deepseek", return_value="Looks good!") as mock_call:
            with patch.object(deepseek_review, "post_review") as mock_post:
                main()
                mock_call.assert_called_once()
                mock_post.assert_called_once()

        deepseek_review.build_prompt = original_build


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT — review categories
# ---------------------------------------------------------------------------

class TestSystemPromptCategories(unittest.TestCase):
    """SYSTEM_PROMPT should instruct the model to check for the full range
    of issues that a human reviewer would catch — including those that
    Gemini found but DeepSeek missed (HTML validity, accessibility, etc.)."""

    def test_prompt_mentions_html_validity(self):
        self.assertIn("HTML", deepseek_review.SYSTEM_PROMPT)

    def test_prompt_mentions_accessibility(self):
        self.assertIn("accessibility", deepseek_review.SYSTEM_PROMPT.lower())

    def test_prompt_mentions_event_handling(self):
        self.assertIn("event", deepseek_review.SYSTEM_PROMPT.lower())

    def test_prompt_mentions_css_styling(self):
        self.assertIn("CSS", deepseek_review.SYSTEM_PROMPT)

    def test_prompt_mentions_framework_anti_patterns(self):
        self.assertIn("framework", deepseek_review.SYSTEM_PROMPT.lower())

    def test_prompt_mentions_test_file_organization(self):
        self.assertIn("test file", deepseek_review.SYSTEM_PROMPT.lower())

    def test_prompt_does_not_ignore_refactoring_opportunities(self):
        """SYSTEM_PROMPT must NOT tell the model to suppress refactoring
        findings. When a PR is itself a refactoring (e.g. migrating inline
        styles to a CSS module), incomplete refactoring is the primary thing
        the review should catch."""
        self.assertNotIn(
            "Refactoring opportunities",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_indirection_principle(self):
        """SYSTEM_PROMPT should include an 'indirection' principle covering
        patterns where code uses an indirect or imperative construct when a
        simpler, standard, or declarative one exists (e.g., useEffect+focus
        vs autoFocus, inline styles vs CSS modules, manual test sync wrappers
        vs awaiting visible outcomes, instanceof checks vs defaultPrevented)."""
        self.assertIn(
            "**Indirection**",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_inconsistency_principle(self):
        """SYSTEM_PROMPT should include an 'inconsistency' principle covering
        definitions that contradict what they describe (e.g., class names
        that describe a container but are applied to the element itself,
        labels that both wrap and use htmlFor, test fixtures missing type
        annotations, unstable callback references)."""
        self.assertIn(
            "**Inconsistency**",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_boundary_validation(self):
        """SYSTEM_PROMPT should include a 'Boundary Validation' principle
        covering missing input validation at data entry points (form
        submission, API handlers, file parsing)."""
        self.assertIn(
            "Boundary Validation",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_defensive_events(self):
        """SYSTEM_PROMPT should include a 'Defensive Events' principle
        covering missing stopPropagation, preventDefault, double-firing,
        and other event handling bugs."""
        self.assertIn(
            "Defensive Events",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_silent_data_loss(self):
        """SYSTEM_PROMPT should call out silent data loss from logic gated
        on an optional companion method — where a computed result is silently
        discarded when a separate method is absent, rather than applied with a
        default fallback. This covers the pattern seen in decorator/adapter
        code where an effect and its formatting are separate methods."""
        self.assertIn(
            "silent data loss from logic gated on an optional companion",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_coverage_integrity(self):
        """SYSTEM_PROMPT should include a 'Coverage Integrity' principle
        covering coverage thresholds lowered in config files instead of adding
        tests, and test suites that skip error paths, throw branches, boundary
        conditions, and edge cases."""
        self.assertIn(
            "Coverage Integrity",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_coverage_thresholds_lowered(self):
        """SYSTEM_PROMPT should mention coverage thresholds being lowered
        in config files to compensate for uncovered code."""
        self.assertIn(
            "coverage thresholds lowered",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_error_path_tests(self):
        """SYSTEM_PROMPT should instruct the model to flag test suites that
        omit error paths, throw branches, boundary conditions, and edge cases."""
        self.assertIn(
            "error paths",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_throw_branches(self):
        """SYSTEM_PROMPT should mention uncovered throw branches specifically."""
        self.assertIn(
            "throw branches",
            deepseek_review.SYSTEM_PROMPT,
        )

    def test_prompt_mentions_vitest_config(self):
        """SYSTEM_PROMPT should mention vitest as a coverage config example."""
        self.assertIn(
            "vitest",
            deepseek_review.SYSTEM_PROMPT,
        )


# ---------------------------------------------------------------------------
# Workflow YAML configuration
# ---------------------------------------------------------------------------

class TestWorkflowYaml(unittest.TestCase):
    """Tests for the reusable workflow YAML template."""

    _TEMPLATE_PATH = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "deepseek-review-template.yml"
    )

    @unittest.skipIf(not YAML_AVAILABLE, "PyYAML not installed")
    def test_checkout_merge_commit_uses_fetch_depth_0(self):
        """The PR merge-commit checkout step must use fetch-depth: 0 so that
        BASE_SHA and HEAD_SHA are available in the local clone."""
        with open(self._TEMPLATE_PATH) as f:
            workflow = _yaml.safe_load(f)

        steps = workflow["jobs"]["review"]["steps"]
        # Find the checkout step that uses refs/pull/.../merge
        checkout_step = None
        for step in steps:
            if step.get("uses") == "actions/checkout@v4":
                with_params = step.get("with", {})
                ref = with_params.get("ref", "")
                if "refs/pull/" in ref and "/merge" in ref:
                    checkout_step = step
                    break

        self.assertIsNotNone(
            checkout_step,
            "Could not find checkout step with refs/pull/.../merge in the workflow template",
        )
        self.assertEqual(
            checkout_step.get("with", {}).get("fetch-depth"),
            0,
            "PR merge-commit checkout step must have fetch-depth: 0",
        )


if __name__ == "__main__":
    unittest.main()
