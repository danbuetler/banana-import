"""build_xlsx must not 500 on a masked credit-card account.

Excel sheet titles forbid \\ / * ? : [ ] and cap at 31 chars. A credit card
statement carries a masked number like '**** **** **** 4417', which used to make
openpyxl raise 'Invalid character * found in sheet title' and 500 the /read step,
so the Operator silently fell back to the legacy extractor (no reconciliation).
"""
from io import BytesIO

import openpyxl

import camt_xlsx


def test_safe_sheet_title_strips_forbidden_chars():
    assert camt_xlsx._safe_sheet_title("**** **** **** 4417") == "4417"
    assert camt_xlsx._safe_sheet_title("A/B:C[D]E*F?G\\H") == "A B C D E F G H"
    assert camt_xlsx._safe_sheet_title("") == "Statement"
    assert camt_xlsx._safe_sheet_title("****") == "Statement"
    # 31-char cap
    assert len(camt_xlsx._safe_sheet_title("x" * 50)) == 31


def test_build_xlsx_masked_card_does_not_raise():
    statements = [{
        "account": "**** **** **** 4417",
        "currency": "USD",
        "opening": -1250.00,
        "closing": -776.49,
        "entries": [
            {"booking_date": "2026-09-03", "amount": -420.00, "description": "AWS"},
            {"booking_date": "2026-09-12", "amount": 1250.00, "description": "Payment received"},
        ],
    }]
    data = camt_xlsx.build_xlsx(statements)  # must NOT raise
    wb = openpyxl.load_workbook(BytesIO(data))
    assert wb.sheetnames == ["4417"], wb.sheetnames
    # forbidden characters never reach a title
    for name in wb.sheetnames:
        assert not (set(name) & set('\\/*?:[]')), name


if __name__ == "__main__":
    test_safe_sheet_title_strips_forbidden_chars()
    test_build_xlsx_masked_card_does_not_raise()
    print("xlsx title tests OK")
