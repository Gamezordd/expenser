"""Currency codes, user-supplied conversion rates, and conversion of transactions."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable

from expenser.models import Transaction

MAX_RATE_DP = 6
_SYMBOLS = {"฿": "THB", "₹": "INR", "€": "EUR", "£": "GBP", "¥": "JPY"}


class CurrencyError(ValueError):
    """Bad currency code, bad rate, or a rate that is missing."""


def normalize_code(s: str | None) -> str:
    """Return an upper-case 3-letter code. Accepts a few symbols (฿ ₹ € £ ¥)."""
    raw = (s or "").strip()
    if raw in _SYMBOLS:
        return _SYMBOLS[raw]
    code = raw.upper()
    if len(code) != 3 or not (code.isascii() and code.isalpha()):
        raise CurrencyError(f"Invalid currency code {raw!r}; expected a 3-letter ISO code such as THB")
    return code


def parse_rate(text: str) -> Decimal:
    """Parse a positive rate with at most 6 decimals; a comma decimal is accepted."""
    s = text.strip().replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CurrencyError(f"{text!r} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise CurrencyError(f"Rate must be greater than 0 (got {text!r})")
    if d.as_tuple().exponent < -MAX_RATE_DP:  # type: ignore[operator]
        raise CurrencyError(f"Rate {text!r} has more than {MAX_RATE_DP} decimal places")
    return d


def parse_rate_args(items: list[str] | None) -> dict[str, Decimal]:
    """Parse CLI values like ``THB=2.58`` or ``THB:2.58``."""
    out: dict[str, Decimal] = {}
    for item in items or []:
        idx = [i for i in (item.find("="), item.find(":")) if i >= 0]
        if not idx:
            raise CurrencyError(f"Malformed --rate {item!r}; expected CODE=RATE, e.g. THB=2.58")
        i = min(idx)
        try:
            code = normalize_code(item[:i])
            out[code] = parse_rate(item[i + 1:])
        except CurrencyError as e:
            raise CurrencyError(f"--rate {item!r}: {e}") from None
    return out


def summarize_currencies(transactions: list[Transaction], default: str) -> dict[str, tuple[int, Decimal]]:
    """code -> (row count, total original amount), in order of first appearance."""
    out: dict[str, tuple[int, Decimal]] = {}
    for t in transactions:
        code = t.original_currency or default
        amt = t.original_amount if t.original_amount is not None else t.amount
        n, total = out.get(code, (0, Decimal("0")))
        out[code] = (n + 1, total + amt)
    return out


def collect_rates(
    currencies: dict[str, tuple[int, Decimal]],
    target: str,
    given: dict[str, Decimal],
    *,
    input_fn: Callable[[str], str] = input,
    console=None,
    interactive: bool,
) -> dict[str, Decimal]:
    """Rates (1 CODE = rate TARGET) for every detected currency other than the target."""
    target = normalize_code(target)
    rates: dict[str, Decimal] = {}
    for code, (count, total) in currencies.items():
        if code == target:
            continue
        if code in given:
            rates[code] = given[code]
            continue
        if not interactive:
            raise CurrencyError(
                f"No conversion rate for {code} (found in {count} transaction(s)). "
                f"Pass --rate {code}=<rate> (1 {code} = <rate> {target})."
            )
        prompt = (
            f"{code} detected in {count} transaction{'s' if count != 1 else ''} "
            f"(total {total:,.2f} {code}). Rate: 1 {code} = ? {target}: "
        )
        while True:
            try:
                answer = input_fn(prompt)
            except (EOFError, KeyboardInterrupt):
                raise CurrencyError(f"No rate entered for {code}") from None
            try:
                rates[code] = parse_rate(answer)
                break
            except CurrencyError as e:
                msg = f"Invalid rate: {e}. Enter a positive number, e.g. 2.58"
                if console is not None:
                    console.print(f"[red]{msg}[/]")
                else:
                    print(msg)
    return rates


def convert(
    transactions: list[Transaction], rates: dict[str, Decimal], target: str
) -> list[Transaction]:
    """Convert rows to the target currency, keeping the original amount/currency/rate."""
    target = normalize_code(target)
    out: list[Transaction] = []
    for t in transactions:
        code = normalize_code(t.original_currency) if t.original_currency else target
        if code == target:
            out.append(t.model_copy(update={"original_amount": None, "original_currency": None, "rate": None}))
            continue
        if code not in rates:
            raise CurrencyError(f"No conversion rate for {code}")
        original = t.original_amount if t.original_amount is not None else t.amount
        rate = rates[code]
        amount = (original * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out.append(
            t.model_copy(
                update={"original_amount": original, "original_currency": code, "rate": rate, "amount": amount}
            )
        )
    return out
