"""Offline tests for the /bridge/book file resolver (DESK-65 multi-client write).

Resolution must find a client's .ac2 anywhere under the broad synced root, handle a
relative path exactly, flag same-named files as ambiguous, and never escape the root.
"""
import os
import tempfile
import unittest

import app


class TestResolveBananaFile(unittest.TestCase):
    def setUp(self):
        # two synced libraries, like "B2B-Sharepoint - Documents" + the Comm site
        self.sp = tempfile.TemporaryDirectory()
        self.comm = tempfile.TemporaryDirectory()
        sp, comm = self.sp.name, self.comm.name
        os.makedirs(os.path.join(sp, "Clients", "3WAG"))
        os.makedirs(os.path.join(sp, "Clients", "Celecor"))
        os.makedirs(os.path.join(sp, "Clients", "Other"))
        # per-client subfolders, a .bak and a staging twin that must be ignored
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2")
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2.bak")
        self._touch(sp, "Clients", "3WAG", "2025_3WAG.ac2.desk-new.ac2")
        self._touch(comm, "Celecor_2025.ac2")          # file in the second root
        # a duplicate basename in two client folders -> must be ambiguous
        self._touch(sp, "Clients", "Celecor", "shared.ac2")
        self._touch(sp, "Clients", "Other", "shared.ac2")
        self._prev = app.BANANA_FILE_ROOTS
        app.BANANA_FILE_ROOTS = [sp, comm]
        app._ac2_index["at"] = 0.0  # force a rebuild against these roots

    def tearDown(self):
        app.BANANA_FILE_ROOTS = self._prev
        app._ac2_index["at"] = 0.0
        self.sp.cleanup()
        self.comm.cleanup()

    def _touch(self, *parts):
        with open(os.path.join(*parts), "wb") as fh:
            fh.write(b"x")

    def test_bare_name_found_in_nested_folder(self):
        p = app._resolve_banana_file("2025_3WAG.ac2")
        self.assertTrue(p.endswith(os.path.join("3WAG", "2025_3WAG.ac2")))

    def test_bare_name_found_in_second_root(self):
        p = app._resolve_banana_file("Celecor_2025.ac2")
        self.assertTrue(p.endswith("Celecor_2025.ac2"))
        self.assertTrue(os.path.isfile(p))

    def test_relative_path_exact(self):
        p = app._resolve_banana_file("Clients/3WAG/2025_3WAG.ac2")
        self.assertTrue(p.endswith(os.path.join("Clients", "3WAG", "2025_3WAG.ac2")))

    def test_missing_is_404(self):
        with self.assertRaises(app.FileResolveError) as cx:
            app._resolve_banana_file("nope.ac2")
        self.assertEqual(cx.exception.status, 404)

    def test_duplicate_basename_is_ambiguous(self):
        with self.assertRaises(app.FileResolveError) as cx:
            app._resolve_banana_file("shared.ac2")
        self.assertEqual(cx.exception.status, 409)
        self.assertIn("Celecor", cx.exception.msg)
        self.assertIn("Other", cx.exception.msg)

    def test_bak_and_staging_twins_ignored(self):
        # only the real file matches; .bak / .desk-new.ac2 are skipped (not ambiguous)
        p = app._resolve_banana_file("2025_3WAG.ac2")
        self.assertTrue(os.path.isfile(p))

    def test_traversal_blocked(self):
        with self.assertRaises(app.FileResolveError) as cx:
            app._resolve_banana_file("../../etc/passwd")
        self.assertIn(cx.exception.status, (400, 404))


if __name__ == "__main__":
    unittest.main()
