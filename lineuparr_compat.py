"""Optional generation of normalized lineup JSON files for Lineuparr."""

import copy
import json
import os
import shutil
from datetime import datetime, timezone

from .name_normalizer import normalize_channel_name


def discover_lineup_files(directory):
    """Return Lineuparr-discoverable lineup filenames in a directory."""
    directory = os.path.realpath(str(directory or "").strip())
    if not directory or not os.path.isdir(directory):
        return []
    try:
        return sorted(
            filename
            for filename in os.listdir(directory)
            if filename.lower().endswith("_lineup.json")
            and os.path.isfile(os.path.join(directory, filename))
        )
    except OSError:
        return []


def discover_lineup_options(directory):
    """Return lineup filenames with friendly labels from their JSON metadata."""
    options = []
    for filename in discover_lineup_files(directory):
        path = os.path.join(directory, filename)
        label = filename
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            package = str(payload.get("package") or "").strip()
            if package:
                label = f"{package} — {filename}"
        except (OSError, ValueError, TypeError):
            pass
        options.append({"value": filename, "label": label})
    return options


def derive_output_name(source_name):
    """Create tidyCH's safe generated filename from a Lineuparr source."""
    suffix = "_lineup.json"
    if not str(source_name or "").lower().endswith(suffix):
        raise ValueError("Lineuparr source filename must end with _lineup.json.")
    return f"{source_name[:-len(suffix)]}_tidych{suffix}"


def parse_lineup_mappings(raw_mappings):
    """Parse ``Profile | source.json | output_lineup.json`` mappings."""
    mappings = []
    errors = []
    seen_outputs = set()
    for line_number, raw_line in enumerate(str(raw_mappings or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            errors.append(
                f"Line {line_number}: use Profile Name | source.json | output_lineup.json."
            )
            continue
        profile_name, source_name, output_name = parts
        if not profile_name:
            errors.append(f"Line {line_number}: profile name is blank.")
            continue
        for label, filename in (("source", source_name), ("output", output_name)):
            if (
                not filename
                or filename != os.path.basename(filename)
                or not filename.lower().endswith(".json")
            ):
                errors.append(
                    f"Line {line_number}: {label} must be a JSON filename without a path."
                )
        if source_name == output_name:
            errors.append(f"Line {line_number}: source and output must be different files.")
        if not output_name.lower().endswith("_lineup.json"):
            errors.append(
                f"Line {line_number}: output must end with _lineup.json so Lineuparr can discover it."
            )
        output_key = output_name.casefold()
        if output_key in seen_outputs:
            errors.append(f"Line {line_number}: duplicate output '{output_name}'.")
        if any(error.startswith(f"Line {line_number}:") for error in errors):
            continue
        seen_outputs.add(output_key)
        mappings.append({
            "profile_name": profile_name,
            "source_name": source_name,
            "output_name": output_name,
        })
    return mappings, errors


def build_normalized_lineup(payload, suffixes):
    """Return a normalized lineup copy, change rows, and collision rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), dict):
        raise ValueError("Lineup JSON must contain a categories object.")
    output = copy.deepcopy(payload)
    changes = []
    collisions = []
    for category, entries in output["categories"].items():
        if not isinstance(entries, list):
            raise ValueError(f"Category '{category}' must contain a channel list.")
        targets = {}
        pending = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            original = str(entry.get("name") or "").strip()
            normalized, removed = normalize_channel_name(original, suffixes)
            target_key = normalized.casefold()
            targets.setdefault(target_key, []).append((index, original, normalized, removed))
            pending.append((index, entry, original, normalized, removed))
        collided_indexes = set()
        for same_target in targets.values():
            distinct_names = {item[1].casefold() for item in same_target}
            if len(same_target) > 1 and len(distinct_names) > 1:
                for index, original, normalized, removed in same_target:
                    collided_indexes.add(index)
                    collisions.append({
                        "category": category,
                        "original_name": original,
                        "normalized_name": normalized,
                        "removed_suffix": removed,
                        "result": "collision",
                    })
        for index, entry, original, normalized, removed in pending:
            if not removed or index in collided_indexes:
                continue
            aliases = entry.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            elif not isinstance(aliases, list):
                aliases = []
            clean_aliases = [
                str(alias).strip() for alias in aliases
                if isinstance(alias, str) and str(alias).strip()
            ]
            if original.casefold() not in {alias.casefold() for alias in clean_aliases}:
                clean_aliases.append(original)
            entry["name"] = normalized
            entry["aliases"] = clean_aliases
            changes.append({
                "category": category,
                "original_name": original,
                "normalized_name": normalized,
                "removed_suffix": removed,
                "result": "changed",
            })
    output["tidych"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "normalized_suffixes": list(suffixes),
    }
    return output, changes, collisions


def write_lineup(directory, output_name, payload):
    """Atomically write a lineup and back up an existing generated file."""
    directory = os.path.realpath(str(directory or "").strip())
    if not directory or not os.path.isdir(directory):
        raise ValueError("Lineuparr plugin directory does not exist.")
    output_path = os.path.realpath(os.path.join(directory, output_name))
    if os.path.dirname(output_path) != directory:
        raise ValueError("Output lineup escapes the configured Lineuparr directory.")
    backup_path = ""
    if os.path.exists(output_path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{output_path}.backup-{stamp}"
        shutil.copy2(output_path, backup_path)
    temporary_path = f"{output_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary_path, output_path)
    return output_path, backup_path
