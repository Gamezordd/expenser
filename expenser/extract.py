"""Claude vision extraction: screenshots -> Transactions."""
from __future__ import annotations

import base64
import datetime as dt
import io
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

from expenser.currency import CurrencyError, normalize_code
from expenser.models import Transaction
from expenser.participants import resolve_name

DEFAULT_MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 16000
MAX_IMAGE_BYTES = int(4.5 * 1024 * 1024)
MAX_LONG_EDGE = 7000
MIN_LONG_EDGE = 2000

_FORMAT_TO_MEDIA = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class ExtractionError(RuntimeError):
    """Raised when extraction fails in a way the user should see."""


# --- API-facing schema (kept JSON-schema-simple) --------------------------------
class ExtractedRow(BaseModel):
    name: str = Field(description="Cleaned-up merchant / description, e.g. 'Albert Heijn'")
    amount: float = Field(description="Positive amount, as shown on screen")
    date: str | None = Field(default=None, description="Transaction date as YYYY-MM-DD, or null")
    currency: str = Field(description="ISO 4217 currency code of this transaction as shown, e.g. THB, EUR, INR")
    participants: list[str] = Field(description="Participant names sharing this expense")
    paid_by: str | None = Field(default=None, description="Who paid, or null")
    notes: str | None = Field(default=None, description="Uncertainty or remarks about this row")


class ExtractionSchema(BaseModel):
    transactions: list[ExtractedRow]
    skipped: list[str] = Field(
        default_factory=list, description="Rows deliberately skipped, each with a short reason"
    )


# --- Result ---------------------------------------------------------------------
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ExtractionResult:
    transactions: list[Transaction]
    notes: dict[int, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    currency: str | None = None
    usage: Usage = field(default_factory=Usage)


# --- Images ---------------------------------------------------------------------
def prepare_image(path: Path) -> tuple[str, str]:
    """Return (media_type, base64 data); downscale/re-encode if too large."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ExtractionError(f"Cannot read image {path}: {e}") from e
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        raise ExtractionError(f"{path} is not a readable image: {e}") from e
    fmt = (img.format or "").upper()
    media = _FORMAT_TO_MEDIA.get(fmt)
    if media is None:
        raise ExtractionError(f"{path}: unsupported image format {fmt or 'unknown'} (use png/jpeg/webp/gif)")
    long_edge = max(img.size)
    if len(raw) <= MAX_IMAGE_BYTES and long_edge <= MAX_LONG_EDGE:
        return media, base64.standard_b64encode(raw).decode()

    # Downscale + re-encode as JPEG until small enough, never below MIN_LONG_EDGE.
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    elif img.mode == "L":
        img = img.convert("RGB")
    target = min(long_edge, MAX_LONG_EDGE)
    while True:
        scale = target / max(img.size)
        resized = img if scale >= 1 else img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS
        )
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_BYTES or target <= MIN_LONG_EDGE:
            break
        target = max(MIN_LONG_EDGE, int(target * 0.8))
    if len(data) > MAX_IMAGE_BYTES:
        raise ExtractionError(f"{path} is still larger than 4.5 MB after downscaling")
    return "image/jpeg", base64.standard_b64encode(data).decode()


# --- Prompt ---------------------------------------------------------------------
SYSTEM_PROMPT = """You extract expense transactions from screenshots of bank or credit-card apps \
and return them as structured data for a shared-expense app.

Rules:
- The screenshots show lists of transactions. They may overlap or contain the same transaction \
more than once; output each real transaction exactly once.
- Ignore account balances, headers, totals, pending-authorization duplicates of settled \
transactions, and incoming transfers/refunds/salary. Put anything skipped or uncertain in the \
top-level `skipped` list (with a short reason) or in the `notes` of the nearest row.
- `name`: a cleaned-up merchant name, e.g. "ALBERT HEIJN 1234 AMSTERDAM NLD" becomes "Albert Heijn".
- `amount`: a positive number (no currency symbol, '.' as decimal separator).
- `date`: normalised to YYYY-MM-DD, or null if unknown. If the year is not shown, infer the most \
plausible one and mention that in `notes`.
- `currency`: REQUIRED ISO 4217 code of the currency the amount is shown in. Infer it from symbols, \
codes and context (฿/THB -> THB, ₹/Rs/INR -> INR, € -> EUR, £ -> GBP, RM -> MYR, S$ -> SGD, ¥ -> JPY or \
CNY by context, etc.). If a screenshot is ambiguous (e.g. a bare "$"), pick the most likely currency \
from context (bank, merchant location, language) and say so in that row's `notes`. Different rows may \
have different currencies; do not convert amounts.
- `participants`: use ONLY names from the participant list, spelled exactly as given.
- Never invent transactions. If text is illegible, skip it and say so in `skipped`."""


def build_user_text(
    participants: list[str], payer: str | None, hint: str | None, source_currency: str | None = None
) -> str:
    lines = [
        "Extract all expense transactions from the attached screenshot(s).",
        "",
        "Participants (use these names verbatim):",
        *[f"- {p}" for p in participants],
        "",
        "Default: every transaction is split between ALL participants listed above (put all of "
        "their names in `participants`) unless the user hint below says otherwise.",
    ]
    if payer:
        lines.append(f'Set `paid_by` to exactly "{payer}" for every transaction unless the hint says otherwise.')
    else:
        lines.append("Leave `paid_by` null unless the hint says who paid.")
    if source_currency:
        lines.append(
            f'All amounts in these screenshots are in {source_currency}. Set `currency` to exactly '
            f'"{source_currency}" for every transaction (this is authoritative).'
        )
    if hint and hint.strip():
        lines += [
            "",
            "<user_hint>",
            hint.strip(),
            "</user_hint>",
            "(The hint above is guidance from the user about these screenshots.)",
        ]
    return "\n".join(lines)


# --- Conversion -----------------------------------------------------------------
def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def _convert(
    parsed: ExtractionSchema, participants: list[str], payer: str | None,
    source_currency: str | None = None,
) -> ExtractionResult:
    txs: list[Transaction] = []
    notes: dict[int, str] = {}
    currencies: list[str] = []

    for row in parsed.transactions:
        note_parts: list[str] = []
        if row.notes:
            note_parts.append(row.notes.strip())
        try:
            amount = abs(Decimal(str(row.amount)))
        except InvalidOperation:
            parsed.skipped.append(f"{row.name}: invalid amount {row.amount!r}")
            continue
        names: list[str] = []
        dropped: list[str] = []
        for n in row.participants:
            r = resolve_name(n, participants)
            if r is None:
                dropped.append(n)
            elif r not in names:
                names.append(r)
        if dropped:
            note_parts.append(f"Unknown participant(s) dropped: {', '.join(dropped)}")
        if not names:
            names = list(participants)
            if dropped or not row.participants:
                note_parts.append("No valid participants; defaulted to ALL")
        paid_by = None
        if row.paid_by:
            paid_by = resolve_name(row.paid_by, participants)
            if paid_by is None:
                note_parts.append(f"Unknown payer dropped: {row.paid_by}")
        if paid_by is None:
            paid_by = payer
        d = _parse_date(row.date)
        if row.date and d is None:
            note_parts.append(f"Unparseable date: {row.date}")
        code = source_currency
        if not code:
            try:
                code = normalize_code(row.currency)
            except CurrencyError:
                code = None
                note_parts.append(f"Unrecognised currency {row.currency!r}")
        if code:
            currencies.append(code)
        try:
            tx = Transaction(
                name=row.name, amount=amount, participants=names, paid_by=paid_by, date=d,
                original_amount=amount if code else None, original_currency=code,
            )
        except ValueError as e:
            parsed.skipped.append(f"{row.name}: {e}")
            continue
        if note_parts:
            notes[len(txs)] = "; ".join(note_parts)
        txs.append(tx)

    currency = max(set(currencies), key=currencies.count) if currencies else None
    return ExtractionResult(transactions=txs, notes=notes, skipped=list(parsed.skipped), currency=currency)


# --- Main entry -----------------------------------------------------------------
def extract_transactions(
    images: list[Path],
    participants: list[str],
    *,
    payer: str | None = None,
    hint: str | None = None,
    model: str | None = None,
    client: anthropic.Anthropic | None = None,
    source_currency: str | None = None,
) -> ExtractionResult:
    if not images:
        raise ExtractionError("No images given")
    if not participants:
        raise ExtractionError("No participants given")
    model = model or os.environ.get("EXPENSER_MODEL") or DEFAULT_MODEL
    if source_currency:
        try:
            source_currency = normalize_code(source_currency)
        except CurrencyError as e:
            raise ExtractionError(str(e)) from e

    content: list[dict] = []
    for p in images:
        media, data = prepare_image(Path(p))
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    content.append({"type": "text", "text": build_user_text(participants, payer, hint, source_currency)})

    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            betas=BETAS,
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            output_format=ExtractionSchema,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise ExtractionError(
            "Authentication failed. Run `expenser auth login` or set ANTHROPIC_API_KEY (e.g. in .env)."
        ) from e
    except anthropic.RateLimitError as e:
        raise ExtractionError("Rate limited by the Claude API; wait a bit and retry.") from e
    except anthropic.APIStatusError as e:
        raise ExtractionError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError(f"Could not reach the Claude API: {e}") from e
    except anthropic.AnthropicError as e:  # e.g. missing API key at client construction
        raise ExtractionError(
            f"{e} (run `expenser auth login` or set ANTHROPIC_API_KEY)"
        ) from e

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise ExtractionError(f"Claude declined to process these images. Details: {details}")
    if response.stop_reason == "max_tokens":
        raise ExtractionError(
            "Response was cut off (max_tokens). Try fewer screenshots per run."
        )
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise ExtractionError("Claude returned no structured output; try again.")

    result = _convert(parsed, participants, payer, source_currency)
    u = getattr(response, "usage", None)
    result.usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
    )
    return result
