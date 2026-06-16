"""Unit tests for dividend_booking — composed double-entry securities-income bookings."""

import dividend_booking as db


# A minimal client profile (mirrors banana_live.get_client_profile shape).
PROFILE = {
    "income_accounts": [
        {"account": "7000", "description": "Non-operating revenue"},
        {"account": "3000", "description": "Earnings from Advertisement"},
    ],
    "asset_accounts": [{"account": "1016", "description": "UBS CHF 0001F"}],
    "wht_account": "1202",
}

# The real UBS Partners Group voucher in 01-Input (gross 5040, VST 1764, net 3276).
PGHN = {
    "is_dividend": True, "doc_type": "dividend",
    "security_name": "Partners Group Holding AG", "isin": "CH0024608827",
    "valor": "2460882", "quantity": 120, "value_date": "27.05.2025",
    "currency": "CHF", "gross_amount": 5040.0, "swiss_withholding_tax": 1764.0,
    "foreign_withholding_tax": None, "net_amount": 3276.0, "issuer_country": "CH",
    "suggested_account": "7000", "suggested_account_reason": "securities income",
}


def test_security_key_prefers_isin():
    assert db.security_key("ch0024608827", "Whatever") == "CH0024608827"
    assert db.security_key("", "Partners Group AG") == "partners group ag"


def test_swiss_dividend_books_three_lines_and_balances():
    row = db.build_booking(PGHN, PROFILE, {}, bank_account="1016")
    assert row["income_account"] == "7000"   # AI suggestion, grounded in chart
    assert row["account_source"] == "ai"
    assert row["swiss_wht"] == 1764.0
    assert row["foreign_wht"] == 0.0
    assert row["balances"] is True
    assert not row["warnings"]

    tsv, included, skipped = db.export_banana_tsv([row])
    assert included == 1 and not skipped
    lines = [l.split("\t") for l in tsv.strip().split("\n")]
    body = lines[1:]
    assert len(body) == 3                      # bank + VST debits, income credit
    doc = body[0][1]
    assert all(r[1] == doc for r in body)      # one shared Doc groups the entry
    # Debit bank (net), debit VST, credit income (gross).
    assert body[0][3] == "1016" and body[0][5] == "3276.00"
    assert body[1][3] == "1202" and body[1][5] == "1764.00"
    assert body[2][4] == "7000" and body[2][5] == "5040.00"
    # Debits sum to the credit.
    assert round(float(body[0][5]) + float(body[1][5]), 2) == float(body[2][5])
    assert body[0][0] == "2025-05-27"          # value_date -> ISO


def test_learned_security_map_overrides_ai():
    smap = {"CH0024608827": {"income_account": "3000", "security_name": "Partners Group Holding AG"}}
    row = db.build_booking(PGHN, PROFILE, smap, bank_account="1016")
    assert row["income_account"] == "3000"
    assert row["account_source"] == "map"


def test_foreign_wht_is_flagged_and_held_back():
    foreign = dict(PGHN, isin="US0378331005", security_name="Apple Inc.",
                   issuer_country="US", gross_amount=100.0,
                   swiss_withholding_tax=None, foreign_withholding_tax=15.0,
                   net_amount=85.0, suggested_account="7000")
    row = db.build_booking(foreign, PROFILE, {}, bank_account="1016")
    assert row["foreign_wht"] == 15.0
    assert any("oreign withholding" in w for w in row["warnings"])
    tsv, included, skipped = db.export_banana_tsv([row])
    assert included == 0
    assert skipped and "foreign" in skipped[0][1]


def test_plain_dividend_no_tax_books_two_lines():
    plain = dict(PGHN, swiss_withholding_tax=None, foreign_withholding_tax=None,
                 gross_amount=200.0, net_amount=200.0)
    row = db.build_booking(plain, PROFILE, {}, bank_account="1016")
    assert row["balances"] is True
    tsv, included, _ = db.export_banana_tsv([row])
    assert included == 1
    body = [l.split("\t") for l in tsv.strip().split("\n")][1:]
    assert len(body) == 2                       # no VST line
    assert body[0][3] == "1016" and body[0][5] == "200.00"
    assert body[1][4] == "7000" and body[1][5] == "200.00"


def test_non_dividend_skipped():
    nondiv = dict(PGHN, is_dividend=False, doc_type="trade_confirmation")
    row = db.build_booking(nondiv, PROFILE, {}, bank_account="1016")
    _, included, skipped = db.export_banana_tsv([row])
    assert included == 0 and skipped


def test_missing_bank_account_blocks_export():
    row = db.build_booking(PGHN, PROFILE, {}, bank_account="")
    _, included, skipped = db.export_banana_tsv([row])
    assert included == 0
    assert any("bank account" in s[1] for s in skipped)
