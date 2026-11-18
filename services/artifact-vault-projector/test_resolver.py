"""Tests for resolver.py: evidence scoring and the Jev decision policy.

Standard library only (no NATS): python3 -m unittest test_resolver
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolver as r  # noqa: E402


def fake_jev(choice, confidence):
    calls = []

    def post(url, body, key, timeout):
        calls.append(body)
        return {"model": "jev-test", "answers": {"target": {"choice": choice, "confidence": confidence,
                                                            "probabilities": {}}}}
    post.calls = calls
    return post


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resolver-test-"))
        self.vault = self.tmp / "vault"
        (self.vault / "Projects").mkdir(parents=True)
        self.code = self.tmp / "code"
        self.registry = {
            "tolt": {"slug": "tolt", "name": "TOLT", "repo_path": str(self.code / "tolt"), "status": "active"},
            "tower-of-lost-things": {"slug": "tower-of-lost-things", "name": "Tower of Lost Things",
                                     "repo_path": str(self.code / "tolt" / "tower-of-lost-things")},
            "lost-things-forge": {"slug": "lost-things-forge", "name": "Lost Things Forge",
                                  "repo_path": str(self.code / "tolt" / "lost-things-forge")},
            "milkshake": {"slug": "milkshake", "name": "Milkshake", "repo_path": str(self.code / "Milkshake")},
            "earshot": {"slug": "earshot", "name": "Earshot", "repo_path": str(self.code / "earshot")},
        }

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def folder(self, name: str) -> Path:
        p = self.vault / "Projects" / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def decide(self, slug, kind="unmapped", mapping=None, registry="default", **kw):
        kw.setdefault("key_fn", lambda: None)
        kw.setdefault("git_search", False)
        return r.decide(slug, kind, vault_root=self.vault, mapping=mapping or {},
                        registry=self.registry if registry == "default" else registry, **kw)

    # ── evidence ──────────────────────────────────────────────────────────

    def test_acronym_finds_camelcase_folder(self):
        self.folder("TowerOfLostThings")
        self.folder("Triumph")
        d = self.decide("tolt")
        self.assertEqual((d.action, d.target), ("map", "Projects/TowerOfLostThings"))
        self.assertEqual(d.decider, "deterministic")

    def test_exact_name_any_case(self):
        self.folder("Milkshake")
        d = self.decide("milkshake")
        self.assertEqual(d.target, "Projects/Milkshake")

    def test_child_follows_mapped_parent(self):
        self.folder("TowerOfLostThings")
        subj = r.build_subject("lost-things-forge", "unmapped", {"tolt": "Projects/TowerOfLostThings"},
                               self.registry, git_search=False)
        self.assertEqual(subj.parents, ["tolt"])
        cands = r.score_candidates(subj, self.vault, "Projects", {"tolt": "Projects/TowerOfLostThings"},
                                   self.registry)
        self.assertEqual(cands[0].target, "Projects/TowerOfLostThings")
        self.assertGreaterEqual(cands[0].score, 0.85)

    def test_git_history_finds_rename_of_unregistered_slug(self):
        repo = self.code / "tolt"
        repo.mkdir(parents=True)
        git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m",
                              "chore: tower-of-dumb-things is now tolt"], check=True)
        self.folder("TowerOfLostThings")
        mapping = {"tolt": "Projects/TowerOfLostThings"}
        subj = r.build_subject("tower-of-dumb-things", "unmapped", mapping, self.registry)
        self.assertEqual(subj.mentioned_by, ["tolt"])
        post = fake_jev("Projects/TowerOfLostThings", 0.9)
        d = self.decide("tower-of-dumb-things", mapping=mapping, git_search=True,
                        key_fn=lambda: "k", post=post)
        self.assertEqual((d.action, d.target), ("map", "Projects/TowerOfLostThings"))
        self.assertIn("git history", post.calls[0]["state"])

    def test_title_note_is_evidence(self):
        self.folder("TowerOfLostThings")
        (self.vault / "Projects" / "TowerOfLostThings" / "Tower of Dumb THings.md").write_text("x")
        subj = r.build_subject("tower-of-dumb-things", "unmapped", {}, self.registry, git_search=False)
        cands = r.score_candidates(subj, self.vault, "Projects", {}, self.registry)
        self.assertEqual(cands[0].target, "Projects/TowerOfLostThings")

    def test_missing_target_finds_moved_folder(self):
        moved = self.vault / "Ideas" / "Projects" / "Milkshake" / "2026-11-14"
        moved.mkdir(parents=True)
        d = self.decide("milkshake", kind="missing-target", mapping={"milkshake": "Projects/Milkshake"})
        self.assertEqual((d.action, d.target), ("map", "Ideas/Projects/Milkshake"))

    # ── Jev policy ────────────────────────────────────────────────────────

    def test_strong_candidate_suppresses_create(self):
        self.folder("TowerOfLostThings")
        post = fake_jev("create:Projects/Tower of Lost Things", 0.99)
        d = self.decide("tower-of-lost-things", key_fn=lambda: "k", post=post)
        criteria = post.calls[0]["questions"]["target"]["criteria"]
        self.assertFalse(any(k.startswith("create:") for k in criteria))
        # Jev's (now invalid) create answer falls back to the unambiguous match:
        self.assertEqual((d.action, d.target), ("map", "Projects/TowerOfLostThings"))

    def test_create_for_registered_project_without_folder(self):
        d = self.decide("earshot", key_fn=lambda: "k", post=fake_jev("create:Projects/Earshot", 0.9))
        self.assertEqual((d.action, d.target), ("create", "Projects/Earshot"))

    def test_no_create_for_unregistered_slug(self):
        post = fake_jev("hold", 0.9)
        d = self.decide("mystery-thing", key_fn=lambda: "k", post=post)
        self.assertEqual(d.action, "hold")
        self.assertFalse(any(k.startswith("create:")
                             for k in post.calls[0]["questions"]["target"]["criteria"]))

    def test_low_confidence_disagreeing_choice_holds(self):
        self.folder("Milkshake")
        self.folder("MilkshakeArchive")
        d = self.decide("milkshake", key_fn=lambda: "k", post=fake_jev("Projects/MilkshakeArchive", 0.5))
        self.assertEqual(d.action, "hold")

    def test_low_confidence_agreeing_choice_maps(self):
        self.folder("Milkshake")
        d = self.decide("milkshake", key_fn=lambda: "k", post=fake_jev("Projects/Milkshake", 0.45))
        self.assertEqual((d.action, d.decider), ("map", "jev"))

    def test_jev_failure_falls_back_to_deterministic(self):
        self.folder("Milkshake")

        def boom(*a, **k):
            raise TimeoutError("jev down")
        d = self.decide("milkshake", key_fn=lambda: "k", post=boom)
        self.assertEqual((d.action, d.decider), ("map", "deterministic"))
        self.assertIn("error", d.jev)

    def test_registry_down_never_creates(self):
        d = self.decide("earshot", registry=None, key_fn=lambda: "k",
                        post=fake_jev("create:Projects/Earshot", 0.99))
        self.assertEqual(d.action, "hold")

    def test_unchanged_evidence_skips_the_question(self):
        post = fake_jev("hold", 0.9)
        first = self.decide("mystery-thing", key_fn=lambda: "k", post=post)
        again = self.decide("mystery-thing", key_fn=lambda: "k", post=post,
                            known_fingerprint=first.fingerprint)
        self.assertEqual(again.action, "skip")
        self.assertEqual(len(post.calls), 1)

    def test_parents_ordered_first(self):
        order = r.order_slugs(["lost-things-forge", "mystery", "tolt"], self.registry)
        self.assertEqual(order, ["tolt", "lost-things-forge", "mystery"])

    # ── review regressions ────────────────────────────────────────────────

    def test_shared_fragments_never_count_as_agreement(self):
        # delodocs must not be filed into DeLoDocs-scout-integration at 0.4.
        self.registry["delodocs"] = {"slug": "delodocs", "name": "DeloDocs",
                                     "repo_path": str(self.code / "DeLoDocs")}
        self.folder("DeLoDocs-scout-integration")
        d = self.decide("delodocs", key_fn=lambda: "k",
                        post=fake_jev("Projects/DeLoDocs-scout-integration", 0.5))
        self.assertEqual(d.action, "hold")
        top = d.candidates[0]
        self.assertLess(top["score"], 0.6)

    def test_prefix_folder_is_real_evidence(self):
        self.registry["infra"] = {"slug": "infra", "name": "Infra", "repo_path": str(self.code / "infra")}
        self.folder("Infrastructure")
        subj = r.build_subject("infra", "unmapped", {}, self.registry, git_search=False)
        cands = r.score_candidates(subj, self.vault, "Projects", {}, self.registry)
        self.assertEqual((cands[0].target, cands[0].score), ("Projects/Infrastructure", 0.6))
        # ...so "create Infra" no longer gets the agreement discount:
        d = self.decide("infra", key_fn=lambda: "k", post=fake_jev("create:Projects/Infra", 0.5))
        self.assertEqual(d.action, "hold")

    def test_unhashable_jev_choice_does_not_crash(self):
        self.folder("Milkshake")
        d = self.decide("milkshake", key_fn=lambda: "k", post=fake_jev(["Projects/Milkshake"], "high"))
        self.assertEqual((d.action, d.decider), ("map", "deterministic"))

    def test_no_create_while_parent_has_no_folder(self):
        post = fake_jev("create:Projects/Lost Things Forge", 0.99)
        d = self.decide("lost-things-forge", key_fn=lambda: "k", post=post)  # tolt unmapped
        self.assertEqual(d.action, "hold")
        self.assertFalse(any(k.startswith("create:")
                             for k in post.calls[0]["questions"]["target"]["criteria"]))

    def test_tied_top_candidates_get_no_agreement_discount(self):
        self.folder("Milkshake")
        self.folder("milkshake-old")  # joined "milkshakeold" starts with "milkshake" (0.6)
        self.folder("MILK-SHAKE")     # joined "milkshake" == exact (1.0)
        subj = r.build_subject("milkshake", "unmapped", {}, self.registry, git_search=False)
        cands = r.score_candidates(subj, self.vault, "Projects", {}, self.registry)
        self.assertEqual([c.score for c in cands[:2]], [1.0, 1.0])
        d = self.decide("milkshake", key_fn=lambda: "k", post=fake_jev(cands[0].target, 0.5))
        self.assertEqual(d.action, "hold")

    def test_short_acronyms_do_not_match(self):
        self.registry["ab"] = {"slug": "ab", "name": "ab", "repo_path": str(self.code / "ab")}
        self.folder("AlphaBeta")
        subj = r.build_subject("ab", "unmapped", {}, self.registry, git_search=False)
        self.assertEqual(r.score_candidates(subj, self.vault, "Projects", {}, self.registry), [])

    def test_exact_name_beats_parent_folder_deterministically(self):
        self.registry["33god"] = {"slug": "33god", "name": "33GOD", "repo_path": str(self.code / "33GOD")}
        self.registry["candybar"] = {"slug": "candybar", "name": "candybar",
                                     "repo_path": str(self.code / "33GOD" / "candybar")}
        self.folder("33GOD")
        self.folder("Candybar")
        d = self.decide("candybar", mapping={"33god": "Projects/33GOD"})
        self.assertEqual((d.action, d.target), ("map", "Projects/Candybar"))

    def test_git_mention_is_whole_token_and_rare(self):
        repo = self.code / "tolt"
        repo.mkdir(parents=True)
        git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "infrastructure tweaks"], check=True)
        self.assertEqual(r.git_mentions("infra", self.registry), [])  # too short anyway
        self.assertEqual(r.git_mentions("structure", self.registry), [])  # substring only

    def test_failed_key_lookup_is_retried_not_cached(self):
        import os
        r._KEY_CACHE.clear()
        r._KEY_FAILED_AT.clear()
        old = os.environ.pop("JEV_API_KEY", None)
        os.environ["ARTIFACT_VAULT_JEV_SECRETS"] = "/nonexistent/jev-secrets"
        try:
            self.assertIsNone(r.jev_key())
            self.assertNotIn("k", r._KEY_CACHE)
            r._KEY_FAILED_AT["t"] -= 301  # past the retry window
            os.environ["JEV_API_KEY"] = "later"
            self.assertEqual(r.jev_key(), "later")
        finally:
            os.environ.pop("ARTIFACT_VAULT_JEV_SECRETS", None)
            os.environ.pop("JEV_API_KEY", None)
            if old is not None:
                os.environ["JEV_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
