"""Identity checks shared by signer and contract forms."""

import re


def validate_taxpayer_id(value, *, cpf_only=False):
    """Validate CPF or numeric/alphanumeric CNPJ, including both check digits."""
    if not isinstance(value, str):
        raise ValueError("Informe um CPF válido." if cpf_only else "Informe um CPF/CNPJ válido.")
    compact = re.sub(r"[.\-/\s]", "", value).upper()
    cpf = bool(re.fullmatch(r"[0-9]{11}", compact))
    cnpj = not cpf_only and bool(re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", compact))
    valid = (cpf or cnpj) and len(set(compact)) > 1
    if valid:
        digits = [ord(char) - ord("0") for char in compact]
        for end in (len(digits) - 2, len(digits) - 1):
            weights = range(end + 1, 1, -1) if cpf else (
                (end - index - 1) % 8 + 2 for index in range(end)
            )
            remainder = sum(
                digit * weight for digit, weight in zip(digits[:end], weights, strict=True)
            ) % 11
            valid = valid and digits[end] == (0 if remainder < 2 else 11 - remainder)
    if not valid:
        raise ValueError("Informe um CPF válido." if cpf_only else "Informe um CPF/CNPJ válido.")
