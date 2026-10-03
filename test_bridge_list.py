"""Offline tests for GET /bridge/list (DESK-81 Banana multi-file picker).

The endpoint lists every .ac2 the bridge sees on disk under BANANA_FILE_ROOTS
(open OR closed), so the desk picker offers the real list. It reuses the same
filesystem index as the resolver: .bak / .desk-new.ac2 twins are skipped, and a
duplicate basename is flagged ambiguous (its rel path disambiguates it). 'open'
status is best-effort — null when the Banana webserver is unreachable.
"""
import os
import tempfile
import unittest

import app
import banana_live


class TestBridgeList(unittest.TestCase):
    def setUp(self):
        self.sp = tempfile.TemporaryDirectory()
        self.comm = tempfile.TemporaryDirectory()
        sp, comm = self.sp.name, self.comm.name
        os.makedirs(os.path.join(sp, "Clients", "3WAG"))
        os.makedirs(os.path.join(sp, "Clients", "Celecor"))
        os.makedirs(os.path.join(sp, "Clients", "Other"))
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2")
        self._touch(sp, "Clients", "3WAG", "2024_3WAG.ac2")
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2.bak")
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2.desk-new.ac2")
        self._touch(comm, "Celecor_2025.ac2")
        # duplicate basename across two folders -> ambiguous
        self._touch(sp, "Clients", "Celecor", "shared.ac2")
        self._touch(sp, "Clients", "Other", "shared.ac2")
        self._prev = app.BANANA_FILE_ROOTS
        app.BANANA_FILE_ROOTS = [sp, comm]
        app._ac2_index["at"] = 0.0
        # default: webserver unreachable -> 'open' is null
        self._prev_list_open = banana_live.list_open_files
        banana_live.list_open_files = self._raise_unavailable
        self.client = app.app.test_client()

    def tearDown(self):
        app.BANANA_FILE_ROOTS = self._prev
        app._ac2_index["at"] = 0.0
        banana_live.list_open_files = self._prev_list_open
        self.sp.cleanup()
        self.comm.cleanup()

    def _touch(self, *parts):
        with open(os.path.join(*parts), "wb") as fh:
            fh.write(b"x")

    @staticmethod
    def _raise_unavailable():
        raise banana_live.BananaUnavailable("webserver down")

    def _get(self):
        r = self.client.get("/bridge/list")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_lists_real_files_only(self):
        data = self._get()
        rels = {f["rel"] for f in data["files"]}
        self.assertIn(os.path.join("Clients", "3WAG", "2025_3WAG.ac2"), rels)
        self.assertIn(os.path.join("Clients", "3WAG", "2024_3WAG.ac2"), rels)
        self.assertIn("Celecor_2025.ac2", rels)  # second root -> bare rel
        self.assertEqual(data["roots"], 2)

    def test_bak_and_staging_twins_excluded(self):
        data = self._get()
        names = [f["name"] for f in data["files"]]
        self.assertNotIn("2025_3WAG.ac2.bak", names)
        self.assertNotIn("2025_3WAG.ac2.desk-new.ac2", names)

    def test_duplicate_basename_flagged_ambiguous(self):
        data = self._get()
        shared = [f for f in data["files"] if f["name"] == "shared.ac2"]
        self.assertEqual(len(shared), 2)
        self.assertTrue(all(f["ambiguous"] for f in shared))
        # each carries its full rel path so the desk can disambiguate
        rels = {f["rel"] for f in shared}
        self.assertIn(os.path.join("Clients", "Celecor", "shared.ac2"), rels)
        self.assertIn(os.path.join("Clients", "Other", "shared.ac2"), rels)

    def test_unique_file_not_ambiguous(self):
        data = self._get()
        f = next(f for f in data["files"] if f["name"] == "2025_3WAG.ac2")
        self.assertFalse(f["ambiguous"])

    def test_open_is_null_when_webserver_down(self):
        data = self._get()
        self.assertTrue(all(f["open"] is None for f in data["files"]))

    def test_open_true_for_open_file(self):
        banana_live.list_open_files = lambda: ["2025_3WAG.ac2"]
        data = self._get()
        by_name = {}
        for f in data["files"]:
            by_name.setdefault(f["name"], f)
        self.assertTrue(by_name["2025_3WAG.ac2"]["open"])
        self.assertFalse(by_name["2024_3WAG.ac2"]["open"])

    def test_no_roots_is_400(self):
        app.BANANA_FILE_ROOTS = []
        r = self.client.get("/bridge/list")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
