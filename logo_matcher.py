"""Repository access and isolated channel-logo matching utilities."""

import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request

try:
    from rapidfuzz import fuzz
except ImportError:
    try:
        from thefuzz import fuzz
    except ImportError:
        import difflib

        class _FuzzFallback:
            @staticmethod
            def ratio(a, b):
                return difflib.SequenceMatcher(None, a, b).ratio() * 100

        fuzz = _FuzzFallback()


LOGGER = logging.getLogger("plugins.tidych.matcher")
IMAGE_EXTENSIONS = (".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp")


def parse_repository_source(raw_source):
    """Return (owner/repository, optional branch) or an error string."""
    raw = str(raw_source or "").strip()
    if not raw:
        return None, "GitHub Repository is blank."
    match = re.fullmatch(
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:@(.+))?", raw
    )
    if not match:
        return None, "Use owner/repository or owner/repository@branch."
    repository = match.group(1)
    branch = (match.group(2) or "").strip().strip("/")
    if match.group(2) is not None and not branch:
        return None, "The repository branch cannot be empty."
    return (repository, branch), None


def resolve_repository_branch(repository, branch=""):
    """Return an explicit branch or discover the public repository default."""
    branch = str(branch or "").strip().strip("/")
    if branch:
        return branch
    url = f"https://api.github.com/repos/{repository}"
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Dispatcharr-Channel-Curator",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
        resolved = str(payload.get("default_branch", "")).strip()
        return resolved or None
    except Exception as exc:
        LOGGER.warning("Failed to resolve default branch for %s: %s", repository, exc)
        return None


def fetch_logo_files(repository, branch, directory):
    """Return image filenames in one public GitHub repository directory."""
    directory = str(directory or "").strip().strip("/")
    if directory and any(part in ("", ".", "..") for part in directory.split("/")):
        return [], "Logo folder contains an invalid path segment."
    encoded_directory = urllib.parse.quote(directory, safe="/")
    contents_path = f"/contents/{encoded_directory}" if encoded_directory else "/contents"
    query = urllib.parse.urlencode({"ref": branch})
    url = f"https://api.github.com/repos/{repository}{contents_path}?{query}"
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Dispatcharr-Channel-Curator",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
        if not isinstance(payload, list):
            return [], "GitHub did not return a directory listing."
        files = [
            item["name"]
            for item in payload
            if item.get("type") == "file"
            and str(item.get("name", "")).lower().endswith(IMAGE_EXTENSIONS)
        ]
        if len(payload) >= 1000:
            LOGGER.warning("GitHub directory listing may be truncated at 1000 entries: %s", directory)
        return files, None
    except Exception as exc:
        return [], f"Could not read '{directory or '/'}': {exc}"


def normalize_name(value, country_code=None, is_filename=False):
    """Normalize a channel name or repository filename for comparison."""
    name = unicodedata.normalize("NFC", str(value or "")).lower().strip()
    if is_filename:
        name = re.sub(r"\.(png|svg|jpg|jpeg|gif|webp)$", "", name, flags=re.IGNORECASE)
        if country_code:
            name = re.sub(
                rf"[-._]{re.escape(country_code.lower())}$",
                "",
                name,
                flags=re.IGNORECASE,
            )
    name = name.replace("-", " ").replace("_", " ")
    name = name.replace("&", " and ").replace("+", " plus ")
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"^channel\s+(?=\d)", "", name)
    if str(country_code or "").lower() == "us":
        name = re.sub(r"\s+east\s*$", "", name)
    name = re.sub(r"\s+(hd|sd|uhd|4k|fhd)\s*$", "", name)
    return re.sub(r"\s+", " ", name).strip()


def match_channel(channel_name, filenames, country_code, threshold=90):
    """Return (filename, score) for the best acceptable repository match."""
    normalized_channel = normalize_name(channel_name, country_code)
    if not normalized_channel:
        return None, 0

    suffix_pattern = re.compile(
        rf"[-._]{re.escape(country_code.lower())}\.(png|svg|jpg|jpeg|gif|webp)$",
        flags=re.IGNORECASE,
    )
    country_files = [filename for filename in filenames if suffix_pattern.search(filename)]
    candidates = country_files or filenames

    best_file = None
    best_score = 0.0
    channel_regions = _regional_tokens(channel_name, country_code)
    protected_tokens = {
        token for token in normalized_channel.split()
        if re.fullmatch(r"[a-z]+\d+", token)
    }
    for filename in candidates:
        normalized_file = normalize_name(filename, country_code, is_filename=True)
        if not normalized_file:
            continue
        if _regional_tokens(filename, country_code) != channel_regions:
            continue
        compact_file = normalized_file.replace(" ", "")
        if protected_tokens and not all(token in compact_file for token in protected_tokens):
            continue
        score = float(fuzz.ratio(normalized_channel, normalized_file))
        if score > best_score:
            best_file = filename
            best_score = score
            if score == 100:
                break
    if best_score < threshold:
        return None, round(best_score, 1)
    return best_file, round(best_score, 1)


def _regional_tokens(value, country_code):
    """Return meaningful feed regions, treating US East as the standard feed."""
    normalized = unicodedata.normalize("NFC", str(value or "")).lower()
    tokens = set(re.findall(r"\b(east|west|pacific)\b", normalized))
    if str(country_code or "").lower() == "us":
        tokens.discard("east")
    return tokens


def build_raw_url(repository, branch, directory, filename):
    """Build a safely encoded raw.githubusercontent.com image URL."""
    raw_path = "/".join(
        part
        for part in (branch.strip("/"), directory.strip("/"), filename)
        if part
    )
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in raw_path.split("/")
    )
    return f"https://raw.githubusercontent.com/{repository}/{encoded_path}"
