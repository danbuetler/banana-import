"""Offline tests for banana_write.py — payload build + self-checks, no engine.

The live round-trip (POST to the Banana engine) is verified separately, with Banana
open, because the engine is Mac-local and off unless Daniel is in the file.
"""
import os
import base64
import unittest

import banana_write as bw

# A real .ac2 to build onto; the verified Lego base file. Test skips if it's absent
# (e.g. on the VPS), staying portable.
LEGO_BASE = "/Users/danielbuetler/ClaudeCode/03-Clients/Mutara/Lets Go Digital/lego_base.ac2"

CHF_LINES = [
    {"date": "2023-05-09", "doc": "1815-00", "description": "LEXcellence Gruendung",
     "debit": "6600", "credit": "2000", "amount": 5180.50},
    {"date": "2023-12-14", "doc": "2022-362", "description": "Heller Domizil 2022",
     "debit": "6500", "credit": "2000", "amount": 2420.00},
]


class TestRowShape(unittest.TestCase):
    def test_chf_row_fields(self):
        row = bw._txn_row(CHF_LINES[0])
        f = row["fields"]
        self.assertEqual(row["operation"], {"name": "add"})
        self.assertEqual(f["Date"], "2023-05-09")
        self.assertEqual(f["Doc"], "1815-00")
        self.assertEqual(f["AccountDebit"], "6600")
        self.assertEqual(f["AccountCredit"], "2000")
        self.assertEqual(f["Amount"], "5180.50")
        self.assertEqual(f["AmountCurrency"], "5180.50")
        self.assertEqual(f["ExchangeCurrency"], "CHF")
        self.assertNotIn("ExchangeRate", f)  # CHF carries no rate

    def test_foreign_currency_row_has_rate(self):
        row = bw._txn_row({"date": "2023-01-01", "debit": "1021", "credit": "2999",
                           "amount": 1908.55, "currency": "USD", "rate": 0.925227})
        f = row["fields"]
        self.assertEqual(f["ExchangeCurrency"], "USD")
        self.assertEqual(f["ExchangeRate"], "0.925227")

    def test_crypto_decimals(self):
        row = bw._txn_row({"date": "2023-01-01", "debit": "1200", "credit": "2999",
                           "amount": 0.6368, "currency": "ETH", "decimals": 8,
                           "rate": 1109.249372})
        self.assertEqual(row["fields"]["Amount"], "0.63680000")

    def test_string_amount_passthrough(self):
        row = bw._txn_row({"date": "2023-01-01", "debit": "1", "credit": "2", "amount": "99.99"})
        self.assertEqual(row["fields"]["Amount"], "99.99")

    def test_missing_key_raises(self):
        with self.assertRaises(bw.BananaWriteError):
            bw._txn_row({"date": "2023-01-01", "debit": "1", "amount": 10})  # no credit


class TestPayload(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(LEGO_BASE):
            self.skipTest("Lego base .ac2 not present here")
        self.base = open(LEGO_BASE, "rb").read()

    def test_payload_structure_and_base64_roundtrip(self):
        p = bw.build_payload(self.base, CHF_LINES, title="Lets Go Digital 2023")
        self.assertEqual(p["data"]["format"], "documentChange")
        du = p["data"]["data"][0]["document"]["dataUnits"][0]
        self.assertEqual(du["nameXml"], "Transactions")
        rows = du["data"]["rowLists"][0]["rows"]
        self.assertEqual(len(rows), 2)
        # the base file survives the base64 round-trip byte-for-byte
        self.assertEqual(base64.b64decode(p["fileType"]["ac2"]), self.base)
        self.assertEqual(p["fileType"]["title"], "Lets Go Digital 2023")

    def test_empty_lines_raises(self):
        with self.assertRaises(bw.BananaWriteError):
            bw.build_payload(self.base, [])

    def test_empty_base_raises(self):
        with self.assertRaises(bw.BananaWriteError):
            bw.build_payload(b"", CHF_LINES)


class TestControlTotal(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(bw.control_total(CHF_LINES), 7600.50)

    def test_post_refuses_on_total_mismatch_before_network(self):
        # With a dummy token set, the control-total guard fires BEFORE any POST,
        # so a wrong expect_total raises without touching the engine.
        old = bw.TOKEN
        bw.TOKEN = "dummy-token-for-test"
        try:
            with self.assertRaises(bw.BananaWriteError) as ctx:
                bw.post_document(b"x" * 10, CHF_LINES, expect_total=9999.99)
            self.assertIn("control total", str(ctx.exception))
        finally:
            bw.TOKEN = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
