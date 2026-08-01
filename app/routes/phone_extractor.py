import re

# Mirrors PhoneNumberExtractor.kt (Android app) exactly, so a number found in an
# outgoing message's text lines up with the same number found in the operator's
# reply text - regardless of which of the operator's own gateway numbers the
# reply is physically sent from.
_NUMBER_REGEX = re.compile(r'(?:\+?91[\s\-]?|0)?(\d{4,15})')
_MOB_REGEX = re.compile(r'MOB\s*[:\-]?\s*(\d{4,15})', re.IGNORECASE)
_DIGITS_ONLY = re.compile(r'[^0-9]')


def normalize(phone):
    """Last 10 digits if the number has at least 10, else just the digits."""
    if not phone:
        return ""
    digits = _DIGITS_ONLY.sub("", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def extract_first(text):
    """
    Returns the first candidate account/mobile number found in `text`,
    normalized. Prioritizes an explicit "MOB: <number>" marker (used to avoid
    accidentally matching other metadata like IMEI/VLR/IMSI/Request-Id in an
    operator's reply); falls back to the first generic number pattern match.
    Returns None if nothing is found.
    """
    if not text:
        return None
    mob_match = _MOB_REGEX.search(text)
    if mob_match:
        return normalize(mob_match.group(1))
    match = _NUMBER_REGEX.search(text)
    if match:
        return normalize(match.group(1))
    return None
