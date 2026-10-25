"""Phone numbers identify local customers; they do not verify identity."""
import re


def normalize_phone(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", value):
        raise ValueError("Please provide a phone number with country code.")
    digits = re.sub(r"\D", "", value)
    if not value.startswith("+"):
        if len(digits) == 10:
            digits = "1" + digits  # Local MVP default: US/Canada.
        elif len(digits) != 11 or not digits.startswith("1"):
            raise ValueError("Please include the country code, for example +1 202 555 0100.")
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError("Invalid phone number.")
    return "+" + digits
