"""
Turn an extracted "Statement of assets" into a Banana year-end revaluation booking.

The securities account in Banana carries a book value. At year-end the holding is
revalued to a target value — either acquisition COST (conservative / historical
cost) or year-end MARKET (CO Art. 960b for listed securities). The CHANGE versus
the current book value is booked to P&L:

    delta = target_value - current_book_value
    delta > 0 (write up):  Debit  <securities account>  /  Credit <gain account>
    delta < 0 (write down): Debit  <loss account>        /  Credit <securities account>

One self-balancing row, no VatCode (a value adjustment carries no VAT).

Output = a Banana "Transactions" import file (tab-separated, column-ID headers):
    Date  Doc  Description  AccountDebit  AccountCredit  Amount  VatCode
"""

import re
from datetime import datetime

EXPORT_COLUMNS = ["Date", "Doc", "Description", "AccountDebit", "AccountCredit", "Amount", "VatCode"]

# Below this the change is treated as zero (nothing to book).
MIN_DELTA = 0.01


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _pos(v):
    n = _num(v)
    return abs(n) if n is not None else None


def _to_iso(date_str):
    """DD.MM.YYYY (or already-ISO) -> YYYY-MM-DD for Banana import. Passthrough on miss."""
    s = str(date_str or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def normalize_positions(extraction):
    """Pull the equity positions into a clean list of reviewable rows."""
    out = []
    for p in (extraction.get("positions") or []):
        cost = _pos(p.get("cost_value"))
        market = _pos(p.get("market_value"))
        upl = _num(p.get("unrealized_pl"))
        if upl is None and cost is not None and market is not None:
            upl = round(market - cost, 2)
        out.append({
            "security": (p.get("security_name") or "").strip(),
            "isin": (p.get("isin") or "").strip().upper(),
            "valor": (p.get("valor") or "").strip(),
            "quantity": p.get("quantity"),
            "position_currency": (p.get("position_currency") or "").strip().upper(),
            "cost_value": cost,
            "market_value": market,
            "unrealized_pl": upl,
        })
    return out


def compute_revaluation(positions, *, basis, current_book, securities_account,
                        gain_account, loss_account, as_of_date, description=""):
    """
    Compute the revaluation booking. Returns a dict with the target/delta summary,
    the booking row (or None if nothing to book) and any warnings. Pure arithmetic
    — never raises on missing optional data; it flags warnings instead.
    """
    basis = (basis or "cost").lower()
    key = "cost_value" if basis == "cost" else "market_value"
    warnings = []

    vals = [p[key] for p in positions if p.get(key) is not None]
    missing = [p["security"] for p in positions if p.get(key) is None]
    if missing:
        warnings.append(f"No {basis} value read for: {', '.join(missing)} — excluded from the target.")
    target = round(sum(vals), 2)

    current = _num(current_book)
    if current is None:
        warnings.append("No current book value for the securities account — enter it to compute the change.")
        delta = None
    else:
        delta = round(target - current, 2)

    basis_label = "acquisition cost" if basis == "cost" else "market value"
    desc = (description or "").strip() or \
        f"Wertschriften-Wertberichtigung per {as_of_date} (zu {('Anschaffungskosten' if basis == 'cost' else 'Kurswert')})"

    booking = None
    if delta is not None and abs(delta) >= MIN_DELTA:
        if not securities_account:
            warnings.append("Pick the securities account before booking.")
        if delta > 0 and not gain_account:
            warnings.append("Pick the gain account (the write-up offset) before booking.")
        if delta < 0 and not loss_account:
            warnings.append("Pick the loss account (the write-down offset) before booking.")
        if delta > 0:
            debit, credit = securities_account, gain_account
        else:
            debit, credit = loss_account, securities_account
        booking = {
            "date": as_of_date,
            "description": desc,
            "account_debit": debit or "",
            "account_credit": credit or "",
            "amount": abs(delta),
            "direction": "write-up" if delta > 0 else "write-down",
        }
    elif delta is not None:
        warnings.append("Target equals the current book value — no revaluation needed.")

    return {
        "basis": basis,
        "basis_label": basis_label,
        "target": target,
        "current_book": current,
        "delta": delta,
        "n_positions": len(positions),
        "booking": booking,
        "warnings": warnings,
    }


def _q(v):
    return "" if v is None else re.sub(r"[\t\r\n]+", " ", str(v)).strip()


def export_banana_tsv(booking):
    """Build the one-row Banana transactions import file from a computed booking."""
    out = ["\t".join(EXPORT_COLUMNS)]
    if not booking:
        return "\n".join(out), 0
    out.append("\t".join([
        _to_iso(booking.get("date")),
        "",
        _q(booking.get("description")),
        _q(booking.get("account_debit")),
        _q(booking.get("account_credit")),
        f"{booking['amount']:.2f}",
        "",
    ]))
    return "\n".join(out), 1
