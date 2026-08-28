"""build_graph() over the fixture (tests/fixtures/repo.json): exact node/edge sets, the closes-shadows-ref
rule, crossref dedup against parsed refs, stub resolution, g.ctx snippets — then apply_filters,
components, focus, subgraph, bfs_tree, pick_root and item_degree on the resulting Graph.

Run directly: python3 -m unittest tests.test_graph -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

REPO = testenv.FIXTURE_REPO  # "test/repo"


def qid(n):
    return f"{REPO}#{n}"


# Exact edge set for the fixture, empirically verified against tests/fixtures/repo.json and cross-checked
# against tests/test_env_selfcheck.py's node/edge counts (15 nodes, 14 edges).
EXPECTED_EDGES = {
    (qid(1), "@alice", "mention"),
    (qid(1), qid(5), "ref"),
    (qid(3), "ghe.example.com/eng/tools#301", "ref"),
    (qid(3), "other/lib#42", "ref"),
    (qid(3), qid(1), "ref"),
    (f"{qid(4)}/c101", "@carol", "mention"),
    (f"{qid(4)}/c101", qid(4), "comment"),
    (qid(5), qid(4), "closes"),
    (f"{qid(5)}/c201", "@alice", "mention"),
    (f"{qid(5)}/c201", qid(5), "comment"),
    (f"{qid(6)}/c301", qid(6), "comment"),
    (f"{qid(6)}/c302", qid(4), "ref"),
    (f"{qid(6)}/c302", qid(6), "comment"),
    (qid(7), qid(2), "ref"),
}

EXPECTED_ITEM_IDS = {qid(1), qid(2), qid(3), qid(4), qid(5), qid(6), qid(7),
                     "other/lib#42", "ghe.example.com/eng/tools#301"}
EXPECTED_COMMENT_IDS = {f"{qid(4)}/c101", f"{qid(5)}/c201", f"{qid(6)}/c301", f"{qid(6)}/c302"}
EXPECTED_PERSON_IDS = {"@alice", "@carol"}


class TestBuildGraphNodesAndEdges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_node_ids_by_kind(self):
        by_kind = {"item": set(), "comment": set(), "person": set()}
        for nid, n in self.g.nodes.items():
            by_kind[n.kind].add(nid)
        self.assertEqual(by_kind["item"], EXPECTED_ITEM_IDS)
        self.assertEqual(by_kind["comment"], EXPECTED_COMMENT_IDS)
        self.assertEqual(by_kind["person"], EXPECTED_PERSON_IDS)

    def test_exact_edge_set(self):
        self.assertEqual(self.g.edges, EXPECTED_EDGES)

    def test_closes_shadows_the_parsed_ref_for_the_same_pair(self):
        # PR #5's body both says "Closes #4" (parseable) and lists #4 in its closes[] field.
        # finalize() must drop the "ref" that "closes" shadows for the same (src, dst) pair.
        self.assertIn((qid(5), qid(4), "closes"), self.g.edges)
        self.assertNotIn((qid(5), qid(4), "ref"), self.g.edges)

    def test_crossref_not_yet_explained_by_parsing_is_added(self):
        # #7 is a same-repo, closed, crossref-only stub: GitHub's timeline says "#7 referenced #2", and
        # nothing in the fixture's parsed bodies produces that pair, so it must show up as a "ref" edge.
        self.assertIn((qid(7), qid(2), "ref"), self.g.edges)

    def test_crossref_does_not_duplicate_a_reference_already_found_by_parsing(self):
        """Regression for the `if (s.id, iid) not in parsed_pairs` guard in build_graph()
        (gitgraph.py, the "incoming cross references" loop right after the main per-item loop).

        Fixture gap: the only crossref in tests/fixtures/repo.json (#7 -> #2) has its source (#7) as a
        stub with no body of its own to parse, so parsed_pairs can never already contain that pair --
        the fixture alone cannot exercise the "already explained" branch. It also can't be exercised by
        edge-set assertions in general: g.edges is a set, so re-adding an identical (src, dst, type)
        tuple is invisible there regardless of the guard. So this drives the real build_graph() with a
        minimal synthetic repo via a mocked load_items(), and spies on Graph.add_edge to prove the
        crossref loop actually skips re-deriving an edge that parsing already produced -- not just that
        the final edge set happens to look right.
        """
        gg = self.gg
        items = [
            {"repo": "x/y", "number": 1, "is_pr": False, "title": "A", "state": "OPEN", "draft": False,
             "body": "see #2", "created": "2024-01-01T00:00:00Z", "updated": "2024-01-01T00:00:00Z",
             "url": "https://example.invalid/x/y/issues/1", "author": "a", "labels": [],
             "comments": [], "comments_total": 0, "crossrefs": [], "closes": []},
            {"repo": "x/y", "number": 2, "is_pr": False, "title": "B", "state": "OPEN", "draft": False,
             "body": "unrelated", "created": "2024-01-02T00:00:00Z", "updated": "2024-01-02T00:00:00Z",
             "url": "https://example.invalid/x/y/issues/2", "author": "b", "labels": [],
             "comments": [], "comments_total": 0,
             "crossrefs": [{"repo": "x/y", "number": 1, "is_pr": False, "title": "A", "state": "OPEN",
                            "draft": False, "created": "2024-01-01T00:00:00Z", "author": "a",
                            "when": "2024-01-02T00:00:00Z"}],
             "closes": []},
        ]
        calls = []
        orig_add_edge = gg.Graph.add_edge

        def spy(self_g, src, dst, typ):
            calls.append((src, dst, typ))
            return orig_add_edge(self_g, src, dst, typ)

        with mock.patch.object(gg, "load_items", return_value=(items, time.time())), \
             mock.patch.object(gg.Graph, "add_edge", spy):
            g = gg.build_graph(["x/y"], "open", 10 ** 8)

        self.assertIn(("x/y#1", "x/y#2", "ref"), g.edges)
        self.assertEqual(calls.count(("x/y#1", "x/y#2", "ref")), 1,
                          "the crossref loop re-derived a ref edge already produced by parsing #1's body")

    def test_stub_items_filled_from_cache_without_a_gh_call(self):
        cases = {
            qid(7): dict(is_pr=False, title="중복 이슈 (구버전)", state="CLOSED", author="eve"),
            "other/lib#42": dict(is_pr=False,
                                  title="allocator: double free in slab reclaim under memory pressure",
                                  state="OPEN", author="frank"),
            "ghe.example.com/eng/tools#301": dict(is_pr=False, title="Internal: track vendor kernel backport",
                                                   state="OPEN", author="grace"),
        }
        for nid, expect in cases.items():
            n = self.g.nodes[nid]
            self.assertTrue(n.stub, nid)
            for attr, val in expect.items():
                self.assertEqual(getattr(n, attr), val, f"{nid}.{attr}")
            # url is never filled by stub resolution -- only title/state/is_pr/draft/created/author/body
            self.assertIsNone(n.url, nid)

    def test_ctx_holds_the_sentence_that_made_a_reference(self):
        g = self.g
        self.assertEqual(g.ctx[(qid(1), qid(5))],
                          "This does not overwrite #5, but you may want to check; see PR #5 for the actual fix.")
        self.assertEqual(g.ctx[(qid(3), "other/lib#42")],
                          "Related to other/lib#42 which tracks the upstream fix for the same allocator bug.")
        self.assertEqual(g.ctx[(qid(5), qid(4))],
                          "Rebuilds the extent list on mount instead of trusting the stale checkpoint from "
                          "before the crash. Closes #4.")
        self.assertEqual(g.ctx[(f"{qid(6)}/c302", qid(4))],
                          "nit: consider reusing the constant from issue #4 instead of a magic number.")
        # a crossref-only edge (no parsed source text) never gets a g.ctx entry
        self.assertNotIn((qid(7), qid(2)), g.ctx)


class TestApplyFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_comments_linked_keeps_only_comments_with_an_outgoing_link(self):
        # c101 (mentions @carol), c201 (mentions @alice), c302 (refs #4) are "linked";
        # c301 (a plain APPROVED review, no refs/mentions) is not and must be dropped.
        cg = self.gg.apply_filters(self.g, "linked", True, True)
        comment_ids = {i for i, n in cg.nodes.items() if n.kind == "comment"}
        self.assertEqual(comment_ids, {f"{qid(4)}/c101", f"{qid(5)}/c201", f"{qid(6)}/c302"})
        self.assertFalse(cg.show_linked is False)  # comments != "none" -> show_linked stays True

    def test_comments_all_keeps_every_comment(self):
        cg = self.gg.apply_filters(self.g, "all", True, True)
        comment_ids = {i for i, n in cg.nodes.items() if n.kind == "comment"}
        self.assertEqual(comment_ids, EXPECTED_COMMENT_IDS)

    def test_comments_none_drops_comments_and_promotes_their_edges_to_the_parent_item(self):
        cg = self.gg.apply_filters(self.g, "none", True, True)
        self.assertFalse(cg.show_linked)
        self.assertEqual({i for i, n in cg.nodes.items() if n.kind == "comment"}, set())
        # c302's "ref #4" and c101's "mention @carol" and c201's "mention @alice" move to their items
        self.assertIn((qid(6), qid(4), "ref"), cg.edges)
        self.assertIn((qid(4), "@carol", "mention"), cg.edges)
        self.assertIn((qid(5), "@alice", "mention"), cg.edges)
        # the original comment-authored edges are gone (their source nodes no longer exist)
        self.assertNotIn((f"{qid(6)}/c302", qid(4), "ref"), cg.edges)

    def test_people_false_removes_person_nodes_and_inlines_direct_item_mentions(self):
        cg = self.gg.apply_filters(self.g, "linked", False, True)
        self.assertEqual({i for i, n in cg.nodes.items() if n.kind == "person"}, set())
        # #1's body directly mentions @alice (an item-level mention edge) -> inlined onto #1 itself
        self.assertEqual(cg.nodes[qid(1)].inline_mentions, ["@alice"])
        # c101/c201's ONLY qualifying link was a mention; with people hidden that edge disappears
        # entirely (it never becomes a real edge to inline), so under "linked" they no longer qualify
        # as linked and are dropped outright.
        self.assertNotIn(f"{qid(4)}/c101", cg.nodes)
        self.assertNotIn(f"{qid(5)}/c201", cg.nodes)

    def test_people_false_with_comments_all_keeps_the_comment_and_inlines_on_it(self):
        cg = self.gg.apply_filters(self.g, "all", False, True)
        self.assertIn(f"{qid(4)}/c101", cg.nodes)
        self.assertEqual(cg.nodes[f"{qid(4)}/c101"].inline_mentions, ["@carol"])
        self.assertEqual(cg.nodes[f"{qid(5)}/c201"].inline_mentions, ["@alice"])

    def test_closed_neighbors_false_drops_only_closed_or_merged_stubs(self):
        cg = self.gg.apply_filters(self.g, "linked", True, False)
        # #7 is a stub AND closed -> dropped
        self.assertNotIn(qid(7), cg.nodes)
        # other/lib#42 and ghe...#301 are stubs but OPEN -> kept
        self.assertIn("other/lib#42", cg.nodes)
        self.assertIn("ghe.example.com/eng/tools#301", cg.nodes)
        # #4 is CLOSED but not a stub (it was fully fetched) -> kept
        self.assertIn(qid(4), cg.nodes)


class TestComponents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_exact_two_components(self):
        comps = self.gg.components(self.g)
        as_sets = sorted((frozenset(c) for c in comps), key=len)
        expected_small = frozenset({qid(2), qid(7)})
        expected_big = frozenset({qid(1), qid(3), qid(4), qid(5), qid(6), "@alice", "@carol",
                                   "other/lib#42", "ghe.example.com/eng/tools#301",
                                   f"{qid(4)}/c101", f"{qid(5)}/c201", f"{qid(6)}/c301", f"{qid(6)}/c302"})
        self.assertEqual(as_sets, [expected_small, expected_big])


class TestFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_focus_one_hop_from_pr5(self):
        got = self.gg.focus(self.g, qid(5), 1)
        self.assertEqual(got, {qid(5), qid(4), qid(1), "@alice", f"{qid(4)}/c101", f"{qid(5)}/c201"})

    def test_focus_two_hops_from_pr5(self):
        got = self.gg.focus(self.g, qid(5), 2)
        self.assertEqual(got, {qid(5), qid(4), qid(1), qid(3), qid(6), "@alice", "@carol",
                                f"{qid(4)}/c101", f"{qid(5)}/c201", f"{qid(6)}/c301", f"{qid(6)}/c302"})

    def test_focus_one_hop_from_pr3(self):
        got = self.gg.focus(self.g, qid(3), 1)
        self.assertEqual(got, {qid(3), qid(1), "other/lib#42", "ghe.example.com/eng/tools#301"})

    def test_focus_zero_hops_still_includes_zero_cost_comment_neighbors(self):
        # comment edges cost 0 (see focus()'s `cost = 0 if t == "comment" else 1`), so hops=0 is not
        # just the root: #5's own comment c201 comes along for free.
        self.assertEqual(self.gg.focus(self.g, qid(5), 0), {qid(5), f"{qid(5)}/c201"})


class TestSubgraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_subgraph_restricts_nodes_and_edges(self):
        ids = {qid(5), qid(4), f"{qid(5)}/c201", "@alice"}
        h = self.gg.subgraph(self.g, ids)
        self.assertEqual(set(h.nodes), ids)
        self.assertEqual(h.edges, {
            (qid(5), qid(4), "closes"),
            (f"{qid(5)}/c201", "@alice", "mention"),
            (f"{qid(5)}/c201", qid(5), "comment"),
        })
        # ids not requested (e.g. #1, which links to #5) must not leak in
        self.assertNotIn(qid(1), h.nodes)


class TestBfsTree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_bfs_tree_small_component(self):
        comps = self.gg.components(self.g)
        comp2 = next(c for c in comps if qid(2) in c)
        parent, pedge, children = self.gg.bfs_tree(self.g, comp2, qid(2))
        self.assertEqual(parent, {qid(2): None, qid(7): qid(2)})
        self.assertEqual(pedge, {qid(2): None, qid(7): ("ref", False)})
        self.assertEqual(dict(children), {qid(2): [qid(7)]})

    def test_bfs_tree_persons_are_never_expanded(self):
        comps = self.gg.components(self.g)
        big = next(c for c in comps if qid(3) in c)
        parent, pedge, children = self.gg.bfs_tree(self.g, big, qid(3))
        # persons are members of the component but must never become tree parents of anything
        self.assertNotIn("@alice", children)
        self.assertNotIn("@carol", children)
        self.assertNotIn("@alice", parent)  # never even reached by the tree walk
        self.assertNotIn("@carol", parent)


class TestPickRootAndItemDegree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_item_degree_matches_the_number_of_distinct_linked_items(self):
        expected = {qid(1): 2, qid(2): 1, qid(3): 3, qid(4): 1, qid(5): 2, qid(6): 1, qid(7): 1,
                    "other/lib#42": 1, "ghe.example.com/eng/tools#301": 1}
        for nid, deg in expected.items():
            self.assertEqual(self.gg.item_degree(self.g, nid), deg, nid)

    def test_pick_root_prefers_the_most_linked_fetched_item(self):
        comps = self.gg.components(self.g)
        for c in comps:
            root = self.gg.pick_root(self.g, c)
            if qid(3) in c:
                self.assertEqual(root, qid(3))  # degree 3, the highest in that component
            else:
                self.assertEqual(root, qid(2))  # #7 is a stub, excluded when a fetched item exists

    def test_pick_root_falls_back_to_a_stub_when_nothing_else_is_fetched(self):
        # a component made only of stubs: pick_root must still return something (the `or items` fallback)
        h = self.gg.subgraph(self.g, {qid(7)})
        root = self.gg.pick_root(h, {qid(7)})
        self.assertEqual(root, qid(7))


if __name__ == "__main__":
    unittest.main()
