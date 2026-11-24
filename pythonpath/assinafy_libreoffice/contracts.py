"""Native contract fields and input parsing; network operations use the SDK."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .validation import validate_taxpayer_id

TERM_FIELDS = (
    ("counterparty_name", "Contraparte", "text"),
    ("counterparty_taxpayer_id", "CPF/CNPJ da contraparte", "taxpayer"),
    ("value", "Valor (ex.: 1200.50)", "money"),
    ("currency", "Moeda (ex.: BRL)", "currency"),
    ("start_date", "Início (AAAA-MM-DD)", "date"),
    ("end_date", "Término (AAAA-MM-DD)", "date"),
    ("renewal_term_months", "Prazo de renovação (meses)", "months"),
    ("notice_period_days", "Aviso prévio (dias)", "days"),
)
RELATIONS = (("amendment", "Aditivo"), ("renewal", "Renovação"), ("termination", "Rescisão"))


def parse_term(text, kind):
    """Parse a native field; blank means an explicit clear, never an omitted edit."""
    text = text.strip()
    if not text:
        return None
    if kind == "money":
        try:
            amount = Decimal(text)
            if not amount.is_finite() or not 0 <= amount <= Decimal("9999999999999.99"):
                raise ValueError
            if amount != amount.quantize(Decimal("0.01")):
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                "Informe um valor positivo ou zero, com até duas casas decimais."
            ) from exc
        return float(amount)
    if kind in {"months", "days"}:
        minimum, maximum = (1, 600) if kind == "months" else (0, 3650)
        if not text.isascii() or not text.isdigit() or not minimum <= int(text) <= maximum:
            raise ValueError(f"Informe um inteiro entre {minimum} e {maximum}.")
        return int(text)
    if kind == "date":
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                raise ValueError
            if date.fromisoformat(text).year < 1000:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "Informe uma data válida no formato AAAA-MM-DD, a partir de 1000."
            ) from exc
    elif kind == "currency":
        if not re.fullmatch(r"[A-Za-z]{3}", text):
            raise ValueError("Informe uma moeda de três letras, como BRL.")
        return text.upper()
    elif kind == "taxpayer":
        validate_taxpayer_id(text)
    elif len(text) > 255:
        raise ValueError("A contraparte deve ter até 255 caracteres.")
    return text
