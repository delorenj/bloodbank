"""Tests for vault_moves.py and the projector's --move-folder / mapping reload.

Standard library only: python3 -m unittest test_vault_moves
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_moves as vm  # noqa: E402

try:
    import main as projector_main  # noqa: E402  (needs jsonschema/nats importable)
except Exception:  # noqa: BLE001
    projector_main = None


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-moves-"))
        self.root = self.tmp / "vault"
        (self.root / "Projects").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_merge_moves_dedupes_and_suffixes(self):
        self.write("Projects/delo.sh/Run Book.md", "runbook")
        self.write("Projects/delo.sh/2026-10-20/a.md", "same")
        self.write("Projects/DeLoNET/2026-10-20/a.md", "same")
        self.write("Projects/delo.sh/2026-10-20/b.md", "mine")
        self.write("Projects/DeLoNET/2026-10-20/b.md", "theirs")
        rep = vm.merge_folder(self.root, "Projects/delo.sh", "Projects/DeLoNET")
        self.assertTrue(rep["src_removed"])
        self.assertEqual((rep["moved"], rep["deduped"]), (2, 1))
        self.assertEqual((self.root / "Projects/DeLoNET/Run Book.md").read_text(), "runbook")
        self.assertEqual((self.root / "Projects/DeLoNET/2026-10-20/b.md").read_text(), "theirs")
        [renamed] = rep["renamed"]
        self.assertEqual((self.root / renamed).read_text(), "mine")
        self.assertFalse((self.root / "Projects/delo.sh").exists())

    def test_creates_missing_destination_leaf_only(self):
        self.write("Projects/HeyMa Satellite/x.md", "x")
        rep = vm.merge_folder(self.root, "Projects/HeyMa Satellite", "Projects/tonnybox")
        self.assertTrue(rep["created"])
        with self.assertRaises(vm.MoveError):
            vm.merge_folder(self.root, "Projects/tonnybox", "Nope/deeper/tonnybox")

    def test_refuses_unsafe_and_nested(self):
        self.write("Projects/A/x.md", "x")
        for src, dst in (("Projects/A", "../out"), ("Projects/A", "Projects/A/B"), ("Projects/A", "Projects/A"),
                         ("/etc", "Projects/B"), ("Projects/Missing", "Projects/B")):
            with self.assertRaises(vm.MoveError, msg=(src, dst)):
                vm.merge_folder(self.root, src, dst)
        self.assertTrue((self.root / "Projects/A/x.md").exists())

    def test_links_follow_moved_files(self):
        self.write("Projects/TalkyTonny/docs/arch.md", "# arch")
        self.write("Projects/TalkyTonny/docs/index.md",
                   "[arch](./arch.md) [up](../../Index.md) [repo](../../../TalkyTonny/trunk-main/x.md)")
        self.write("Index.md", "[[Projects/TalkyTonny/docs/arch]] [[arch]] "
                               "[a](Projects/TalkyTonny/docs/arch.md#top) [web](https://delo.sh/x)")
        self.write("Projects/Other/n.md", "[a](../TalkyTonny/docs/arch.md) ![img](<../TalkyTonny/docs/arch.md>)")
        rep = vm.merge_folder(self.root, "Projects/TalkyTonny", "Projects/tonnybox")
        moves = rep["moves"]
        changed = vm.rewrite_links(self.root, moves)
        self.assertEqual(sorted(changed), ["Index.md", "Projects/Other/n.md"])
        self.assertEqual((self.root / "Index.md").read_text(),
                         "[[Projects/tonnybox/docs/arch]] [[arch]] "
                         "[a](Projects/tonnybox/docs/arch.md#top) [web](https://delo.sh/x)")
        self.assertEqual((self.root / "Projects/Other/n.md").read_text(),
                         "[a](../tonnybox/docs/arch.md) ![img](<../tonnybox/docs/arch.md>)")
        # Same depth, same siblings: the moved note is not rewritten at all (its
        # "./arch.md" spelling survives), and the out-of-vault link is untouched.
        self.assertEqual((self.root / "Projects/tonnybox/docs/index.md").read_text(),
                         "[arch](./arch.md) [up](../../Index.md) [repo](../../../TalkyTonny/trunk-main/x.md)")

    def test_moved_note_relative_links_are_rebased(self):
        self.write("Projects/HeyMa Satellite/2026-10-18/r.md", "[top](../../HeyMa/Plan.md)")
        self.write("Projects/HeyMa/Plan.md", "plan")
        self.write("Projects/Deep/Nest/.keep", "")
        rep = vm.merge_folder(self.root, "Projects/HeyMa Satellite", "Projects/Deep/Nest/tonnybox")
        vm.rewrite_links(self.root, rep["moves"])
        self.assertEqual((self.root / "Projects/Deep/Nest/tonnybox/2026-10-18/r.md").read_text(),
                         "[top](../../../../HeyMa/Plan.md)")

    def test_space_paths_are_encoded(self):
        self.write("Projects/DeLoNET Company/2026-10-20/r.md", "r")
        self.write("Index.md", "[r](Projects/DeLoNET%20Company/2026-10-20/r.md)")
        rep = vm.merge_folder(self.root, "Projects/DeLoNET Company", "Projects/Delo Net")
        vm.rewrite_links(self.root, rep["moves"])
        self.assertEqual((self.root / "Index.md").read_text(), "[r](Projects/Delo%20Net/2026-10-20/r.md)")


@unittest.skipIf(projector_main is None, "projector dependencies not importable")
class ProjectorMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-moves-proj-"))
        self.root = self.tmp / "vault"
        (self.root / "Projects" / "DeloDocs" / "2026-10-18").mkdir(parents=True)
        self.file = self.root / "Projects" / "DeloDocs" / "2026-10-18" / "reply.md"
        self.file.write_text("hi")
        self.mapping = self.tmp / "mapping.json"
        self.mapping.write_text(json.dumps({"$comment": "keep", "delodocs": "Projects/DeloDocs",
                                            "bb": "Projects/Bloodbank"}))
        self.state = projector_main.ProjectorState(self.tmp / "state.sqlite3")
        self.state.record({"event_id": "e1", "artifact_id": "a", "revision": 1, "project_slug": "delodocs",
                           "vault_path": str(self.file), "projected_at": "t"})
        writer = projector_main.VaultWriter(self.root, projector_main.load_mapping(str(self.mapping)))
        self.proj = projector_main.Projector(state=self.state, writer=writer, store_root=self.tmp / "store",
                                             tz="America/New_York", mapping_path=self.mapping)

    def tearDown(self):
        self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_folder_remaps_slugs_and_projection_rows(self):
        rep = self.proj.move_folder("Projects/DeloDocs", "Projects/ObsOps")
        self.assertEqual(rep["remapped"], ["delodocs"])
        self.assertEqual(rep["projection_rows"], 1)
        raw = json.loads(self.mapping.read_text())
        self.assertEqual(raw["delodocs"], "Projects/ObsOps")
        self.assertEqual(raw["$comment"], "keep")
        self.assertEqual(self.proj.writer.mapping["delodocs"], "Projects/ObsOps")
        db = sqlite3.connect(self.tmp / "state.sqlite3")
        [(path,)] = db.execute("SELECT vault_path FROM projections").fetchall()
        self.assertEqual(path, str(self.root / "Projects/ObsOps/2026-10-18/reply.md"))
        last = self.state.last_resolution("delodocs")
        self.assertEqual((last["decider"], last["target"]), ("operator", "Projects/ObsOps"))

    def test_running_projector_picks_up_mapping_edits(self):
        import os
        raw = json.loads(self.mapping.read_text())
        raw["pjangler"] = "Projects/33GOD"
        self.mapping.write_text(json.dumps(raw))
        os.utime(self.mapping, ns=(1, 1))  # a different stamp even within one mtime tick
        self.assertTrue(self.proj.refresh_mapping())
        self.assertEqual(self.proj.writer.mapping["pjangler"], "Projects/33GOD")
        self.mapping.write_text("{not json")
        os.utime(self.mapping, ns=(2, 2))
        self.proj.refresh_mapping()  # invalid: logged, previous mapping kept
        self.assertEqual(self.proj.writer.mapping["pjangler"], "Projects/33GOD")


if __name__ == "__main__":
    unittest.main()
