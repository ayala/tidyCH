"""Safe, profile-scoped channel name normalization helpers."""

import re


SUFFIX_RE = re.compile(r"[A-Za-z0-9]+")


def parse_name_rules(raw_rules):
    """Parse ``Profile Name | HD, FHD`` rules."""
    rules = []
    errors = []
    seen_profiles = set()
    for line_number, raw_line in enumerate(str(raw_rules or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 2:
            errors.append(
                f"Line {line_number}: use Profile Name | suffix, suffix."
            )
            continue
        profile_name, raw_suffixes = parts
        if not profile_name:
            errors.append(f"Line {line_number}: profile name is blank.")
            continue
        profile_key = profile_name.casefold()
        if profile_key in seen_profiles:
            errors.append(f"Line {line_number}: duplicate profile '{profile_name}'.")
            continue
        suffixes = []
        for raw_suffix in raw_suffixes.split(","):
            suffix = raw_suffix.strip()
            if not suffix:
                continue
            if not SUFFIX_RE.fullmatch(suffix):
                errors.append(
                    f"Line {line_number}: suffix '{suffix}' must contain only letters and numbers."
                )
                continue
            if suffix.casefold() not in {item.casefold() for item in suffixes}:
                suffixes.append(suffix.upper())
        if not suffixes:
            errors.append(f"Line {line_number}: add at least one suffix.")
            continue
        seen_profiles.add(profile_key)
        rules.append({
            "profile_name": profile_name,
            "suffixes": tuple(sorted(suffixes, key=str.casefold)),
        })
    return rules, errors


def normalize_channel_name(name, suffixes):
    """Remove one configured trailing quality marker from ``name``.

    The marker must be a separate trailing token, optionally parenthesized or
    preceded by a dash. Interior text is never changed.
    """
    original = str(name or "").strip()
    if not original or not suffixes:
        return original, ""
    suffix_group = "|".join(
        sorted((re.escape(value) for value in suffixes), key=len, reverse=True)
    )
    pattern = re.compile(
        rf"^(?P<base>.+?)(?:\s+|\s*[-\u2013\u2014]\s*)"
        rf"(?:\((?P<paren>{suffix_group})\)|(?P<plain>{suffix_group}))\s*$",
        flags=re.IGNORECASE,
    )
    match = pattern.match(original)
    if not match:
        return original, ""
    cleaned = re.sub(r"\s+", " ", match.group("base")).strip(" -\u2013\u2014")
    if not cleaned:
        return original, ""
    removed = match.group("paren") or match.group("plain") or ""
    return cleaned, removed.upper()

