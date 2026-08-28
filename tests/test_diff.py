"""Unit tests for the PR review pipeline's first half (gitgraph.py "PR review" section, 0.22.0):
parse_unified_diff(), where a finding may be anchored, the findings cache, and the git worktree gg
checks a PR head out into.

Safety: the worktree tests run real `git`, so every one of them points gg.CACHE_DIR at a throwaway
directory first and asserts it landed there — review_worktree() creates and DELETES directory trees,
and it must never be aimed at the developer's real ~/.cache/gitgraph. gg.CHECKOUTS is pre-seeded and
gg._scanned_for_checkouts is set so nothing in here walks the developer's filesystem looking for
clones, and clone_remote_for() is stubbed to the local bare repo so no test touches the network.

Run: python3 -m unittest tests.test_diff -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()


# --------------------------------------------------------------------------- parsing
SIMPLE = """\
diff --git a/fs/f2fs/data.c b/fs/f2fs/data.c
index 1111111..2222222 100644
--- a/fs/f2fs/data.c
+++ b/fs/f2fs/data.c
@@ -220,6 +220,7 @@ static int f2fs_write_page(struct page *page)
 	if (!page)
-		return -ENOMEM;
+		goto out_unlock;
+
 	spin_lock(&sbi->lock);
 out_unlock:
 	spin_unlock(&sbi->lock);
 	return 0;
"""


class TestParseUnifiedDiff(unittest.TestCase):
    def test_one_file_one_hunk(self):
        files = gg.parse_unified_diff(SIMPLE)
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertEqual(f.path, "fs/f2fs/data.c")
        self.assertEqual(f.old_path, "fs/f2fs/data.c")
        self.assertEqual(f.status, "modified")
        self.assertEqual((f.additions, f.deletions), (2, 1))
        self.assertEqual(len(f.hunks), 1)

    def test_line_numbers_follow_the_hunk_header(self):
        h = gg.parse_unified_diff(SIMPLE)[0].hunks[0]
        self.assertEqual((h.old_start, h.old_lines, h.new_start, h.new_lines), (220, 6, 220, 7))
        self.assertEqual(h.heading, "static int f2fs_write_page(struct page *page)")
        got = [(tag, o, n) for tag, o, n, _ in h.lines]
        self.assertEqual(got[:4], [(" ", 220, 220), ("-", 221, None), ("+", None, 221), ("+", None, 222)])
        self.assertEqual(got[-1], (" ", 225, 226))

    def test_hunk_header_round_trips(self):
        self.assertEqual(gg.parse_unified_diff(SIMPLE)[0].hunks[0].header,
                         "@@ -220,6 +220,7 @@ static int f2fs_write_page(struct page *page)")

    def test_touched_lines_are_the_changed_ones_only(self):
        f = gg.parse_unified_diff(SIMPLE)[0]
        self.assertEqual(f.touched("RIGHT"), {221, 222})
        self.assertEqual(f.touched("LEFT"), {221})

    def test_multiple_hunks_and_files(self):
        text = SIMPLE + """\
diff --git a/fs/f2fs/gc.c b/fs/f2fs/gc.c
--- a/fs/f2fs/gc.c
+++ b/fs/f2fs/gc.c
@@ -10,2 +10,3 @@
 a
+b
 c
@@ -88,1 +89,1 @@ gc_thread_func
-old
+new
"""
        files = gg.parse_unified_diff(text)
        self.assertEqual([f.path for f in files], ["fs/f2fs/data.c", "fs/f2fs/gc.c"])
        gc = files[1]
        self.assertEqual(len(gc.hunks), 2)
        self.assertEqual((gc.additions, gc.deletions), (2, 1))
        self.assertEqual(gc.touched("RIGHT"), {11, 89})

    def test_added_file(self):
        f = gg.parse_unified_diff("""\
diff --git a/new.c b/new.c
new file mode 100644
index 0000000..abcdefg
--- /dev/null
+++ b/new.c
@@ -0,0 +1,2 @@
+one
+two
""")[0]
        self.assertEqual(f.status, "added")
        self.assertEqual(f.path, "new.c")
        self.assertEqual((f.additions, f.deletions), (2, 0))
        self.assertEqual(f.touched("RIGHT"), {1, 2})
        self.assertEqual(f.touched("LEFT"), set())

    def test_deleted_file(self):
        f = gg.parse_unified_diff("""\
diff --git a/gone.c b/gone.c
deleted file mode 100644
--- a/gone.c
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
""")[0]
        self.assertEqual(f.status, "deleted")
        self.assertEqual(f.path, "gone.c")
        self.assertEqual((f.additions, f.deletions), (0, 2))

    def test_rename_without_content_change_has_both_paths(self):
        f = gg.parse_unified_diff("""\
diff --git a/old/name.c b/new/name.c
similarity index 100%
rename from old/name.c
rename to new/name.c
""")[0]
        self.assertEqual(f.status, "renamed")
        self.assertEqual(f.old_path, "old/name.c")
        self.assertEqual(f.path, "new/name.c")
        self.assertEqual(f.hunks, [])

    def test_rename_with_edits(self):
        f = gg.parse_unified_diff("""\
diff --git a/a.c b/b.c
similarity index 90%
rename from a.c
rename to b.c
--- a/a.c
+++ b/b.c
@@ -1 +1 @@
-x
+y
""")[0]
        self.assertEqual((f.status, f.old_path, f.path), ("renamed", "a.c", "b.c"))
        self.assertEqual((f.additions, f.deletions), (1, 1))

    def test_no_newline_marker_is_not_a_content_line(self):
        f = gg.parse_unified_diff("""\
diff --git a/x.c b/x.c
--- a/x.c
+++ b/x.c
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
""")[0]
        self.assertTrue(f.no_newline)
        self.assertEqual([t for t, _, _, _ in f.hunks[0].lines], ["-", "+"])
        self.assertEqual((f.additions, f.deletions), (1, 1))

    def test_binary_file(self):
        f = gg.parse_unified_diff("""\
diff --git a/logo.png b/logo.png
index 111..222 100644
Binary files a/logo.png and b/logo.png differ
""")[0]
        self.assertTrue(f.binary)
        self.assertEqual(f.path, "logo.png")
        self.assertEqual(f.hunks, [])

    def test_crlf_is_stripped_from_content(self):
        f = gg.parse_unified_diff("diff --git a/x.c b/x.c\r\n--- a/x.c\r\n+++ b/x.c\r\n"
                                  "@@ -1 +1 @@\r\n-a\r\n+b\r\n")[0]
        self.assertEqual(f.path, "x.c")
        self.assertEqual([t for _, _, _, t in f.hunks[0].lines], ["a", "b"])

    def test_empty_context_line_counts_as_context(self):
        """git writes a bare empty line for an empty context line; it must not end the hunk."""
        f = gg.parse_unified_diff("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n"
                                  "@@ -1,3 +1,4 @@\n a\n\n+b\n c\n")[0]
        self.assertEqual([t for t, _, _, _ in f.hunks[0].lines], [" ", " ", "+", " "])
        self.assertEqual(f.additions, 1)

    def test_content_line_that_looks_like_a_file_header(self):
        """A removed line reading '-- foo' arrives as '--- foo'; inside a hunk it is content."""
        f = gg.parse_unified_diff("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n"
                                  "@@ -1,2 +1,2 @@\n--- old signature\n+++ new signature\n z\n")[0]
        self.assertEqual(f.path, "x.c")
        self.assertEqual([(t, txt) for t, _, _, txt in f.hunks[0].lines],
                         [("-", "-- old signature"), ("+", "++ new signature"), (" ", "z")])

    def test_hunk_header_without_counts_means_one_line(self):
        h = gg.parse_unified_diff("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n"
                                  "@@ -5 +5 @@\n-a\n+b\n")[0].hunks[0]
        self.assertEqual((h.old_lines, h.new_lines), (1, 1))
        self.assertEqual(h.header, "@@ -5 +5 @@")

    def test_path_with_a_space(self):
        f = gg.parse_unified_diff("diff --git a/dir/my file.c b/dir/my file.c\n"
                                  "--- a/dir/my file.c\n+++ b/dir/my file.c\n"
                                  "@@ -1 +1 @@\n-a\n+b\n")[0]
        self.assertEqual(f.path, "dir/my file.c")

    def test_cjk_content_survives_intact(self):
        f = gg.parse_unified_diff("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n"
                                  "@@ -1 +1 @@\n-/* 존을 연다 */\n+/* zone을 연다 */\n")[0]
        self.assertEqual([t for _, _, _, t in f.hunks[0].lines], ["/* 존을 연다 */", "/* zone을 연다 */"])

    def test_empty_input(self):
        self.assertEqual(gg.parse_unified_diff(""), [])
        self.assertEqual(gg.parse_unified_diff(None), [])


# --------------------------------------------------------------------------- anchoring
class TestAnchoring(unittest.TestCase):
    def review(self, findings):
        rv = gg.Review("test/repo", 1)
        rv.files = gg.parse_unified_diff(SIMPLE)
        rv.findings = findings
        return gg.anchor_findings(rv)

    def test_a_changed_line_anchors_as_is(self):
        f = gg.Finding(path="fs/f2fs/data.c", line=222, side="RIGHT", title="t")
        self.review([f])
        self.assertEqual((f.anchor, f.line), ("ok", 222))

    def test_a_line_outside_the_hunk_is_pulled_to_the_nearest_changed_one(self):
        f = gg.Finding(path="fs/f2fs/data.c", line=226, side="RIGHT", title="t")
        self.review([f])
        self.assertEqual((f.anchor, f.line), ("moved", 222))

    def test_a_path_not_in_the_diff_is_unanchored(self):
        f = gg.Finding(path="fs/f2fs/nowhere.c", line=1, side="RIGHT", title="t")
        self.review([f])
        self.assertEqual(f.anchor, "unanchored")
        self.assertFalse(f.postable)

    def test_left_side_uses_the_removed_lines(self):
        f = gg.Finding(path="fs/f2fs/data.c", line=221, side="LEFT", title="t")
        self.review([f])
        self.assertEqual((f.anchor, f.line), ("ok", 221))

    def test_a_file_with_no_changed_line_on_that_side_is_unanchored(self):
        rv = gg.Review("test/repo", 1)
        rv.files = gg.parse_unified_diff("diff --git a/new.c b/new.c\nnew file mode 100644\n"
                                         "--- /dev/null\n+++ b/new.c\n@@ -0,0 +1 @@\n+one\n")
        f = gg.Finding(path="new.c", line=1, side="LEFT", title="t")
        rv.findings = [f]
        gg.anchor_findings(rv)
        self.assertEqual(f.anchor, "unanchored")

    def test_a_non_numeric_line_still_anchors(self):
        f = gg.Finding(path="fs/f2fs/data.c", line="not a number", side="RIGHT", title="t")
        self.review([f])
        self.assertEqual((f.anchor, f.line), ("moved", 221))

    def test_only_anchored_open_findings_are_postable(self):
        ok = gg.Finding(path="fs/f2fs/data.c", line=222, side="RIGHT", title="a")
        false = gg.Finding(path="fs/f2fs/data.c", line=222, side="RIGHT", title="b", verdict="FALSE")
        posted = gg.Finding(path="fs/f2fs/data.c", line=222, side="RIGHT", title="c", state="posted")
        self.review([ok, false, posted])
        self.assertEqual([f.postable for f in (ok, false, posted)], [True, False, False])


class TestDigest(unittest.TestCase):
    def test_same_defect_same_key_regardless_of_spacing_and_case(self):
        self.assertEqual(gg.finding_digest("a.c", "Lock  leak on the error path"),
                         gg.finding_digest("a.c", "lock leak on the error path"))

    def test_a_different_file_is_a_different_key(self):
        self.assertNotEqual(gg.finding_digest("a.c", "lock leak"), gg.finding_digest("b.c", "lock leak"))

    def test_a_finding_gets_its_digest_and_id_for_free(self):
        f = gg.Finding(path="a.c", title="lock leak")
        self.assertEqual(f.digest, gg.finding_digest("a.c", "lock leak"))
        self.assertEqual(f.fid, f.digest)


class TestPrTarget(unittest.TestCase):
    def test_forms(self):
        repos = ["test/repo"]
        for s, want in [("123", ("test/repo", 123)),
                        ("#123", ("test/repo", 123)),
                        ("other/proj#7", ("other/proj", 7)),
                        ("https://github.com/other/proj/pull/9", ("other/proj", 9))]:
            self.assertEqual(gg.parse_pr_target(s, repos), want, s)

    def test_a_bare_repo_inherits_the_primary_host(self):
        self.assertEqual(gg.parse_pr_target("o/n#4", ["ghe.example.com/a/b"]),
                         ("ghe.example.com/o/n", 4))

    def test_garbage_is_refused(self):
        with self.assertRaises(ValueError):
            gg.parse_pr_target("not a pr", ["test/repo"])


# --------------------------------------------------------------------------- cache + worktree
class ReviewTempCase(unittest.TestCase):
    """Points gg.CACHE_DIR at a throwaway dir and stops anything here from scanning for clones."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gg-review-test-")
        self.cache = os.path.join(self.tmp, ".cache", "gitgraph")
        os.makedirs(self.cache)
        self._old_cache = gg.CACHE_DIR
        self._old_checkouts = dict(gg.CHECKOUTS)
        self._old_scanned = gg._scanned_for_checkouts
        gg.CACHE_DIR = self.cache
        gg.CHECKOUTS.clear()
        gg._scanned_for_checkouts = True        # never walk the developer's filesystem from a test
        self.addCleanup(self._restore)
        # the point of the whole file: never operate on the real cache
        self.assertTrue(gg.CACHE_DIR.startswith(tempfile.gettempdir()))
        self.assertNotIn(os.path.expanduser("~/.cache/gitgraph"), gg.CACHE_DIR)

    def _restore(self):
        gg.CACHE_DIR = self._old_cache
        gg.CHECKOUTS.clear()
        gg.CHECKOUTS.update(self._old_checkouts)
        gg._scanned_for_checkouts = self._old_scanned
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestReviewCache(ReviewTempCase):
    def review(self, head_oid="a" * 40):
        rv = gg.Review("test/repo", 5, head_oid=head_oid)
        rv.files = gg.parse_unified_diff(SIMPLE)
        return rv

    def test_saved_review_comes_back_for_the_same_head(self):
        rv = self.review()
        rv.findings = [gg.Finding(path="fs/f2fs/data.c", line=222, title="lock leak", severity="bug")]
        path = gg.save_review(rv)
        self.assertTrue(path.startswith(self.cache))
        got = gg.cached_review("test/repo", 5, "a" * 40)
        self.assertEqual([f["title"] for f in got["findings"]], ["lock leak"])
        self.assertIsNone(gg.cached_review("test/repo", 5, "b" * 40))

    def test_posted_and_ignored_survive_a_new_head(self):
        rv = self.review()
        rv.findings = [gg.Finding(path="fs/f2fs/data.c", line=222, title="lock leak", state="posted",
                                  thread_url="https://x/1"),
                       gg.Finding(path="fs/f2fs/data.c", line=222, title="naming", state="ignored")]
        gg.save_review(rv)
        later = self.review(head_oid="b" * 40)
        later.findings = [gg.Finding(path="fs/f2fs/data.c", line=222, title="Lock leak"),
                          gg.Finding(path="fs/f2fs/data.c", line=222, title="naming"),
                          gg.Finding(path="fs/f2fs/data.c", line=222, title="brand new")]
        gg.apply_history(later)
        self.assertEqual([f.state for f in later.findings], ["posted", "ignored", "new"])
        self.assertEqual(later.findings[0].thread_url, "https://x/1")

    def test_a_disproved_finding_is_not_rediscovered(self):
        rv = self.review()
        rv.findings = [gg.Finding(path="fs/f2fs/data.c", line=222, title="bogus",
                                  verdict="FALSE", verdict_reason="caller holds the lock")]
        gg.save_review(rv)
        later = self.review(head_oid="b" * 40)
        later.findings = [gg.Finding(path="fs/f2fs/data.c", line=222, title="bogus")]
        gg.apply_history(later)
        self.assertEqual(later.findings[0].verdict, "FALSE")
        self.assertEqual(later.findings[0].verdict_reason, "caller holds the lock")

    def test_counts(self):
        rv = self.review()
        rv.findings = [gg.Finding(path="a", title="1", verdict="CONFIRMED"),
                       gg.Finding(path="a", title="2", verdict="PLAUSIBLE"),
                       gg.Finding(path="a", title="3", verdict="FALSE"),
                       gg.Finding(path="a", title="4", state="posted"),
                       gg.Finding(path="a", title="5", state="ignored")]
        self.assertEqual(rv.counts(), {"open": 2, "confirmed": 1, "plausible": 1,
                                       "posted": 1, "ignored": 1, "dropped": 1})

    def test_findings_round_trip_through_json(self):
        f = gg.Finding(path="a.c", line=3, title="t", body="b", severity="bug", evidence="e",
                       verdict="CONFIRMED", diff="--- a\n+++ b\n")
        back = gg.Finding.from_json(f.to_json())
        self.assertEqual([back.path, back.line, back.title, back.severity, back.verdict, back.diff],
                         ["a.c", 3, "t", "bug", "CONFIRMED", "--- a\n+++ b\n"])


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class TestWorktree(ReviewTempCase):
    """Real git, entirely inside the temp home: a bare 'remote' carrying refs/pull/7/head, and a clone
    of it standing in for the user's checkout."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        work = os.path.join(self.tmp, "seed")
        os.makedirs(work)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True, capture_output=True)
        for name, text in (("f.c", "one\ntwo\n"), ("f.c", "one\nTWO\nthree\n")):
            with open(os.path.join(work, name), "w") as fh:
                fh.write(text)
            subprocess.run(["git", "-C", work, "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", work, "commit", "-qm", text.split("\n")[1]],
                           check=True, capture_output=True, env=env)
        self.base = git(work, "rev-parse", "HEAD~1")
        self.head = git(work, "rev-parse", "HEAD")
        subprocess.run(["git", "clone", "-q", "--bare", work, self.origin], check=True, capture_output=True)
        git(self.origin, "update-ref", "refs/heads/main", self.base)
        git(self.origin, "update-ref", "refs/pull/7/head", self.head)
        self.clone = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True)
        gg.CHECKOUTS["test/repo"] = self.clone
        self._old_remote = gg.clone_remote_for
        gg.clone_remote_for = lambda clone, repo: self.origin   # no network from a test
        self.addCleanup(lambda: setattr(gg, "clone_remote_for", self._old_remote))

    def test_worktree_lands_in_the_cache_with_the_pr_head(self):
        clone, wt, mb = gg.review_worktree("test/repo", 7, "main")
        self.assertEqual(clone, self.clone)
        self.assertTrue(wt.startswith(self.cache), wt)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), self.head)
        self.assertEqual(mb, self.base)
        for d in (gg.worktrees_dir(), os.path.dirname(wt), wt):   # private code: 0700, like the cache
            self.assertEqual(oct(os.stat(d).st_mode)[-3:], "700", d)

    def test_the_diff_is_the_pr_change_only(self):
        clone, wt, mb = gg.review_worktree("test/repo", 7, "main")
        files = gg.parse_unified_diff(gg.review_diff(clone, mb, gg.pr_ref("test/repo", 7)))
        self.assertEqual([f.path for f in files], ["f.c"])
        self.assertEqual((files[0].additions, files[0].deletions), (2, 1))
        self.assertEqual(files[0].touched("RIGHT"), {2, 3})

    def test_an_existing_worktree_at_the_same_head_is_reused(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        stamp = os.path.join(wt, "gg-was-here")
        open(stamp, "w").close()
        _, wt2, _ = gg.review_worktree("test/repo", 7, "main")
        self.assertEqual(wt, wt2)
        self.assertTrue(os.path.exists(stamp), "the worktree was rebuilt even though the head is the same")

    def test_a_moved_head_rebuilds_the_worktree(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        stamp = os.path.join(wt, "gg-was-here")
        open(stamp, "w").close()
        git(self.origin, "update-ref", "refs/pull/7/head", self.base)
        _, wt2, _ = gg.review_worktree("test/repo", 7, "main")
        self.assertEqual(wt, wt2)
        self.assertFalse(os.path.exists(stamp))
        self.assertEqual(git(wt2, "rev-parse", "HEAD"), self.base)

    def test_pr_refs_are_namespaced_per_repo(self):
        gg.review_worktree("test/repo", 7, "main")
        self.assertIn("refs/gg/test__repo/pr-7", git(self.clone, "show-ref"))
        gg.drop_pr_refs(self.clone, "test/repo", 7)
        self.assertNotIn("refs/gg/test__repo/pr-7", git(self.clone, "show-ref"))

    def test_drop_worktree_also_clears_the_clone_metadata(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        self.assertIn(wt, git(self.clone, "worktree", "list"))
        gg.drop_worktree(wt, self.clone)
        self.assertFalse(os.path.isdir(wt))
        self.assertNotIn(wt, git(self.clone, "worktree", "list"))

    def test_worktree_entries_and_cache_listing_see_it(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        self.assertEqual([(r, n) for r, n, _, _ in gg.worktree_entries()], [("test/repo", "7")])
        listed = {name: group for name, _, _, _, _, group, _ in gg.cache_files()}
        self.assertEqual(listed.get("worktrees/test/repo#7"), "review")

    def test_prune_drops_a_stale_worktree_but_keeps_the_current_one(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        old = os.path.join(gg.worktrees_dir(), "test__repo", "pr-99")
        os.makedirs(old)
        t = time.time() - (gg.WORKTREE_KEEP_DAYS + 1) * 86400
        os.utime(old, (t, t))
        self.assertEqual(gg.prune_worktrees(keep=wt), 1)
        self.assertFalse(os.path.isdir(old))
        self.assertTrue(os.path.isdir(wt))

    def test_prune_keeps_at_most_worktree_max(self):
        _, wt, _ = gg.review_worktree("test/repo", 7, "main")
        for i in range(gg.WORKTREE_MAX + 2):
            d = os.path.join(gg.worktrees_dir(), "test__repo", f"pr-{100 + i}")
            os.makedirs(d)
            t = time.time() - (i + 1) * 60
            os.utime(d, (t, t))
        gg.prune_worktrees(keep=wt)
        self.assertLessEqual(len(gg.worktree_entries()), gg.WORKTREE_MAX + 1)
        self.assertTrue(os.path.isdir(wt))

    def test_no_clone_says_so_instead_of_guessing(self):
        gg.CHECKOUTS.clear()
        with self.assertRaises(ValueError) as cm:
            gg.review_worktree("other/nowhere", 1, "main")
        self.assertIn("no local clone of other/nowhere", str(cm.exception))


class TestCloneRemote(ReviewTempCase):
    def test_the_remote_pointing_at_the_repo_wins_over_origin(self):
        clone = os.path.join(self.tmp, "fork")
        subprocess.run(["git", "init", "-q", clone], check=True, capture_output=True)
        git(clone, "remote", "add", "origin", "https://github.com/me/fork.git")
        git(clone, "remote", "add", "upstream", "https://github.com/them/proj.git")
        self.assertEqual(gg.clone_remote_for(clone, "them/proj"), "upstream")
        self.assertEqual(gg.clone_remote_for(clone, "me/fork"), "origin")

    def test_an_unknown_repo_falls_back_to_its_https_url(self):
        clone = os.path.join(self.tmp, "plain")
        subprocess.run(["git", "init", "-q", clone], check=True, capture_output=True)
        self.assertEqual(gg.clone_remote_for(clone, "a/b"), "https://github.com/a/b.git")
        self.assertEqual(gg.clone_remote_for(clone, "ghe.example.com/a/b"),
                         "https://ghe.example.com/a/b.git")


if __name__ == "__main__":
    unittest.main()
