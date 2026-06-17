"""Unit tests for portfolio_booking — year-end securities revaluation."""

import portfolio_booking as pb

# The real LUT statement positions (cost + market in CHF).
EXTRACTION = {
    "as_of_date": "31.12.2025", "reporting_currency": "CHF",
    "positions": [
        {"security_name": "Partners Group Holding AG", "isin": "CH0024608827", "quantity": 120,
         "cost_value": 91800, "market_value": 117888, "unrealized_pl": 26088},
        {"security_name": "UBS Group Inc", "isin": "CH0244767585", "quantity": 500,
         "cost_value": 7620, "market_value": 18480},
        {"security_name": "Strategy Inc", "isin": "US5949724083", "quantity": 148,
         "position_currency": "USD", "cost_value": 17734, "market_value": 17817},
    ],
}


def test_normalize_fills_unrealized_pl():
    pos = pb.normalize_positions(EXTRACTION)
    assert len(pos) == 3
    ubs = next(p for p in pos if p["isin"] == "CH0244767585")
    assert ubs["unrealized_pl"] == 10860.0   # 18480 - 7620, derived


def test_revalue_to_cost_writes_up_against_lower_book():
    pos = pb.normalize_positions(EXTRACTION)
    r = pb.compute_revaluation(pos, basis="cost", current_book=93436.84,
                               securities_account="1060", gain_account="6950",
                               loss_account="6951", as_of_date="31.12.2025")
    assert r["target"] == 117154.0           # 91800 + 7620 + 17734
    assert r["delta"] == 23717.16            # 117154 - 93436.84
    b = r["booking"]
    assert b["direction"] == "write-up"
    assert b["account_debit"] == "1060" and b["account_credit"] == "6950"
    assert b["amount"] == 23717.16

    tsv, n = pb.export_banana_tsv(b)
    assert n == 1
    row = tsv.split("\n")[1].split("\t")
    assert row[0] == "2025-12-31" and row[1] == ""      # date ISO, no Doc
    assert row[3] == "1060" and row[4] == "6950" and row[5] == "23717.16"
    assert row[6] == ""                                  # no VatCode


def test_revalue_to_market_writes_up_more():
    pos = pb.normalize_positions(EXTRACTION)
    r = pb.compute_revaluation(pos, basis="market", current_book=93436.84,
                               securities_account="1060", gain_account="6950",
                               loss_account="6951", as_of_date="31.12.2025")
    assert r["target"] == 154185.0
    assert r["delta"] == 60748.16
    assert r["booking"]["direction"] == "write-up"


def test_write_down_when_book_above_target():
    pos = pb.normalize_positions(EXTRACTION)
    r = pb.compute_revaluation(pos, basis="cost", current_book=130000.0,
                               securities_account="1060", gain_account="6950",
                               loss_account="6951", as_of_date="31.12.2025")
    assert r["delta"] == -12846.0            # 117154 - 130000
    b = r["booking"]
    assert b["direction"] == "write-down"
    assert b["account_debit"] == "6951" and b["account_credit"] == "1060"
    assert b["amount"] == 12846.0


def test_no_change_books_nothing():
    pos = pb.normalize_positions(EXTRACTION)
    r = pb.compute_revaluation(pos, basis="cost", current_book=117154.0,
                               securities_account="1060", gain_account="6950",
                               loss_account="6951", as_of_date="31.12.2025")
    assert r["delta"] == 0.0
    assert r["booking"] is None
    _, n = pb.export_banana_tsv(r["booking"])
    assert n == 0


def test_missing_book_value_flags_warning():
    pos = pb.normalize_positions(EXTRACTION)
    r = pb.compute_revaluation(pos, basis="cost", current_book=None,
                               securities_account="1060", gain_account="6950",
                               loss_account="6951", as_of_date="31.12.2025")
    assert r["delta"] is None and r["booking"] is None
    assert any("current book value" in w for w in r["warnings"])
