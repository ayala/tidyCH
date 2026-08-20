"""Dispatcharr tidyCH plugin."""

import csv
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from django.db import transaction

from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership, Logo
from apps.plugins.models import PluginConfig
from core.utils import send_websocket_update

from .logo_matcher import (
    build_raw_url,
    fetch_logo_files,
    match_channel,
    parse_repository_source,
    resolve_repository_branch,
)
from .lineuparr_compat import (
    build_normalized_lineup,
    derive_output_name,
    discover_lineup_options,
    parse_lineup_mappings,
    write_lineup,
)
from .name_normalizer import normalize_channel_name, parse_name_rules


LOGGER = logging.getLogger("plugins.tidych")
LOG_PREFIX = "[tidyCH]"
EXPORTS_DIRECTORY = "/data/exports"
PROGRESS_FILE = "/data/channel_curator_progress.json"
LOCK_FILE = "/data/channel_curator_operation.lock"
CANCEL_FILE = "/data/channel_curator_cancel.request"
HISTORY_FILE = "/data/channel_curator_name_history.json"
DEFAULT_LINEUPARR_DIRECTORY = "/data/plugins/lineuparr"
LINEUPARR_PROFILE_FIELD_PREFIX = "lineuparr_lineup_profile_"


class Plugin:
    def __init__(self):
        self._thread = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()

    @property
    def fields(self):
        try:
            profiles = list(ChannelProfile.objects.order_by("name"))
        except Exception:
            profiles = []
        profile_hint = ", ".join(profile.name for profile in profiles) if profiles else "none detected"
        lineup_options = discover_lineup_options(DEFAULT_LINEUPARR_DIRECTORY)
        legacy_selections = {}
        profile_countries = {}
        try:
            saved_settings = PluginConfig.objects.get(key="channel_curator").settings or {}
            saved_profile_mappings, _ = self._parse_mappings(
                saved_settings.get("profile_mappings")
            )
            profile_countries = {
                mapping["profile_name"].casefold(): mapping["country"].upper()
                for mapping in saved_profile_mappings
            }
            legacy_mappings, _ = parse_lineup_mappings(
                saved_settings.get("lineuparr_lineup_mappings")
            )
            legacy_selections = {
                mapping["profile_name"].casefold(): mapping["source_name"]
                for mapping in legacy_mappings
            }
        except Exception:
            pass
        lineup_profile_fields = []
        if lineup_options:
            for profile in profiles:
                country = profile_countries.get(profile.name.casefold(), "")
                relevant_options = [
                    option for option in lineup_options
                    if not country or option["value"].upper().startswith(f"{country}_")
                ]
                selectable_lineups = [
                    {"value": "", "label": "Not used for this profile"},
                    *relevant_options,
                ]
                lineup_profile_fields.append({
                    "id": f"{LINEUPARR_PROFILE_FIELD_PREFIX}{profile.id}",
                    "label": f"{profile.name} Lineup",
                    "type": "select",
                    "default": legacy_selections.get(profile.name.casefold(), ""),
                    "options": selectable_lineups,
                    "help_text": (
                        "Optional. Choose the same Lineup File used for this profile "
                        "in Lineuparr. tidyCH creates a separate *_tidych_lineup.json "
                        "copy automatically and never changes the original."
                        + (f" Showing {country} lineups based on the logo mapping above." if country else "")
                    ),
                })
        else:
            lineup_profile_fields.append({
                "id": "_lineuparr_not_found",
                "type": "info",
                "label": "No Lineuparr lineups found",
                "help_text": (
                    f"No *_lineup.json files were found in {DEFAULT_LINEUPARR_DIRECTORY}. "
                    "You can ignore this section when Lineuparr is not installed."
                ),
            })
        return [
            {
                "id": "_sec_source",
                "type": "info",
                "label": "Logo Repository",
                "help_text": "Use one public GitHub repository and map each Dispatcharr channel profile to its country folder.",
            },
            {
                "id": "github_repository",
                "label": "GitHub Repository",
                "type": "string",
                "default": "",
                "placeholder": "owner/repository",
                "help_text": "Public GitHub repository. Its default branch is automatic. Advanced: owner/repository@branch.",
            },
            {
                "id": "profile_mappings",
                "label": "Channel Profile Folder Mappings",
                "type": "text",
                "default": "",
                "placeholder": "US Profile | us | logos/us\nUK Profile | uk | logos/uk",
                "help_text": (
                    "One mapping per line: Profile Name | two-letter country code | repository folder. "
                    f"Only enabled profile channels are processed. Available profiles: {profile_hint}."
                ),
            },
            {
                "id": "replace_existing_logos",
                "label": "Replace Existing Logos When Matched",
                "type": "boolean",
                "default": False,
                "help_text": "When off, channels that already have a logo are skipped. When on, replacement occurs only after a repository match; unmatched logos are never cleared.",
            },
            {
                "id": "match_threshold",
                "label": "Match Threshold",
                "type": "number",
                "default": 90,
                "min": 70,
                "max": 100,
                "step": 1,
                "help_text": "Minimum filename similarity from 70 to 100. Higher values reduce false matches.",
            },
            {
                "id": "_sec_names",
                "type": "info",
                "label": "Channel Name Normalization",
                "help_text": "Optional profile-scoped cleanup of trailing quality markers. This is independent from logo assignment.",
            },
            {
                "id": "name_normalization_rules",
                "label": "Profile Name Rules",
                "type": "text",
                "default": "",
                "placeholder": "UK Profile | HD\nAnother Profile | HD, FHD",
                "help_text": (
                    "One rule per line: Profile Name | comma-separated trailing markers. "
                    "Only separate markers at the end of a channel name are removed. "
                    f"Available profiles: {profile_hint}."
                ),
            },
            {
                "id": "_sec_lineuparr",
                "type": "info",
                "label": "Optional Lineuparr Compatibility",
                "help_text": (
                    "For each profile, optionally choose the same lineup selected in "
                    "Lineuparr. 'Full' and 'Simple' are different lineup variants supplied "
                    "by Lineuparr—not processing steps. Use the Docs link beside tidyCH's "
                    "name to browse the lineup library on GitHub. tidyCH does not require Lineuparr."
                ),
            },
            *lineup_profile_fields,
        ]

    def run(self, action, params, context):
        logger = context.get("logger", LOGGER)
        settings = context.get("settings", {})
        handlers = {
            "validate_settings": self._validate_settings,
            "preview_logo_matches": self._preview_logo_matches,
            "assign_logos": self._assign_logos,
            "preview_name_changes": self._preview_name_changes,
            "apply_name_changes": self._apply_name_changes,
            "rollback_name_changes": self._rollback_name_changes,
            "generate_lineuparr_lineups": self._generate_lineuparr_lineups,
            "plugin_status": self._plugin_status,
            "cancel_operation": self._cancel_operation,
        }
        handler = handlers.get(action)
        if not handler:
            return {"status": "error", "message": f"Unknown action: {action}"}
        try:
            return handler(settings, logger)
        except Exception as exc:
            logger.exception("%s Action %s failed", LOG_PREFIX, action)
            return {"status": "error", "message": str(exc)}

    @staticmethod
    def _parse_mappings(raw_mappings):
        mappings = []
        errors = []
        seen_profiles = set()
        for line_number, raw_line in enumerate(str(raw_mappings or "").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3:
                errors.append(f"Line {line_number}: use Profile Name | country | folder.")
                continue
            profile_name, country_code, directory = parts
            if not profile_name:
                errors.append(f"Line {line_number}: profile name is blank.")
                continue
            if profile_name.casefold() in seen_profiles:
                errors.append(f"Line {line_number}: duplicate profile '{profile_name}'.")
                continue
            if not re.fullmatch(r"[A-Za-z]{2}", country_code):
                errors.append(f"Line {line_number}: country must be a two-letter code.")
                continue
            directory = directory.strip("/")
            if not directory or any(part in ("", ".", "..") for part in directory.split("/")):
                errors.append(f"Line {line_number}: folder is invalid.")
                continue
            seen_profiles.add(profile_name.casefold())
            mappings.append({
                "profile_name": profile_name,
                "country": country_code.lower(),
                "directory": directory,
            })
        if not mappings and not errors:
            errors.append("Add at least one channel profile folder mapping.")
        return mappings, errors

    def _resolve_configuration(self, settings, fetch_files=True):
        source, source_error = parse_repository_source(settings.get("github_repository"))
        mappings, mapping_errors = self._parse_mappings(settings.get("profile_mappings"))
        errors = list(mapping_errors)
        if source_error:
            errors.insert(0, source_error)
            return None, errors

        repository, configured_branch = source
        branch = resolve_repository_branch(repository, configured_branch)
        if not branch:
            errors.append(f"Could not determine the default branch for {repository}.")
            return None, errors

        profiles_by_name = {
            profile.name.casefold(): profile
            for profile in ChannelProfile.objects.all()
        }
        resolved_mappings = []
        for mapping in mappings:
            profile = profiles_by_name.get(mapping["profile_name"].casefold())
            if not profile:
                errors.append(f"Channel profile not found: {mapping['profile_name']}.")
                continue
            files = []
            if fetch_files:
                files, folder_error = fetch_logo_files(
                    repository, branch, mapping["directory"]
                )
                if folder_error:
                    errors.append(
                        f"{mapping['profile_name']} ({mapping['directory']}): {folder_error}"
                    )
                    continue
                if not files:
                    errors.append(
                        f"{mapping['profile_name']} ({mapping['directory']}): no image files found."
                    )
                    continue
            resolved_mappings.append({**mapping, "profile": profile, "files": files})

        if errors:
            return None, errors
        try:
            threshold = int(settings.get("match_threshold", 90))
        except (TypeError, ValueError):
            threshold = 90
        threshold = min(100, max(70, threshold))
        return {
            "repository": repository,
            "branch": branch,
            "mappings": resolved_mappings,
            "threshold": threshold,
            "replace_existing": bool(settings.get("replace_existing_logos", False)),
        }, []

    def _validate_settings(self, settings, logger):
        details = []
        errors = []

        logo_configured = bool(
            str(settings.get("github_repository") or "").strip()
            or str(settings.get("profile_mappings") or "").strip()
        )
        if logo_configured:
            configuration, logo_errors = self._resolve_configuration(
                settings, fetch_files=True
            )
            errors.extend(logo_errors)
            if configuration:
                details.append(
                    f"Logo repository: branch '{configuration['branch']}'"
                )
                for mapping in configuration["mappings"]:
                    enabled_count = ChannelProfileMembership.objects.filter(
                        channel_profile=mapping["profile"], enabled=True
                    ).count()
                    details.append(
                        f"Logos — {mapping['profile'].name}: "
                        f"{mapping['country'].upper()} / {mapping['directory']} "
                        f"({len(mapping['files'])} logos, {enabled_count} enabled channels)"
                    )
        else:
            details.append("Logo mapping: not configured")

        rules, rule_errors = self._resolve_name_rules(settings)
        errors.extend(rule_errors)
        if rules:
            for rule in rules:
                enabled_count = ChannelProfileMembership.objects.filter(
                    channel_profile=rule["profile"], enabled=True
                ).count()
                details.append(
                    f"Names — {rule['profile'].name}: remove trailing "
                    f"{', '.join(rule['suffixes'])} ({enabled_count} enabled channels)"
                )
        else:
            details.append("Name normalization: not configured")

        lineup_mappings, lineup_errors = self._resolve_lineuparr_mappings(
            settings, rules, require_files=True
        )
        errors.extend(lineup_errors)
        if lineup_mappings:
            for mapping in lineup_mappings:
                details.append(
                    f"Lineuparr — {mapping['profile_name']}: "
                    f"{mapping['source_name']} → {mapping['output_name']}"
                )
        else:
            details.append("Lineuparr compatibility: not configured")

        if errors:
            return {"status": "error", "message": "\n".join(errors)}
        return {
            "status": "ok",
            "message": "Settings validated.\n" + "\n".join(details),
        }

    @staticmethod
    def _profile_lookup():
        return {
            profile.name.casefold(): profile
            for profile in ChannelProfile.objects.all()
        }

    def _resolve_name_rules(self, settings):
        parsed_rules, errors = parse_name_rules(
            settings.get("name_normalization_rules")
        )
        profiles = self._profile_lookup()
        resolved = []
        for rule in parsed_rules:
            profile = profiles.get(rule["profile_name"].casefold())
            if not profile:
                errors.append(
                    f"Channel profile not found in name rules: {rule['profile_name']}."
                )
                continue
            resolved.append({**rule, "profile": profile})
        return resolved, errors

    def _resolve_lineuparr_mappings(self, settings, rules, require_files):
        profile_fields_present = any(
            str(key).startswith(LINEUPARR_PROFILE_FIELD_PREFIX)
            for key in settings
        )
        errors = []
        mappings = []
        if profile_fields_present:
            profiles_by_id = {
                str(profile.id): profile
                for profile in ChannelProfile.objects.all()
            }
            seen_sources = set()
            for key, source_name in settings.items():
                if not str(key).startswith(LINEUPARR_PROFILE_FIELD_PREFIX):
                    continue
                source_name = str(source_name or "").strip()
                if not source_name:
                    continue
                profile_id = str(key)[len(LINEUPARR_PROFILE_FIELD_PREFIX):]
                profile = profiles_by_id.get(profile_id)
                if not profile:
                    errors.append(f"Lineuparr selection references missing profile ID {profile_id}.")
                    continue
                if source_name != os.path.basename(source_name) or not source_name.lower().endswith("_lineup.json"):
                    errors.append(f"Invalid Lineuparr lineup selected for {profile.name}.")
                    continue
                source_key = source_name.casefold()
                if source_key in seen_sources:
                    errors.append(
                        f"{source_name} is selected for more than one profile. "
                        "Choose a lineup only once so generated copies cannot conflict."
                    )
                    continue
                seen_sources.add(source_key)
                mappings.append({
                    "profile_name": profile.name,
                    "source_name": source_name,
                    "output_name": derive_output_name(source_name),
                })
        else:
            # Backward compatibility for tidyCH 0.3.x saved mappings.
            mappings, errors = parse_lineup_mappings(
                settings.get("lineuparr_lineup_mappings")
            )
        directory = str(
            settings.get("lineuparr_plugin_directory")
            or DEFAULT_LINEUPARR_DIRECTORY
        ).strip()
        if not mappings:
            return [], errors
        directory = os.path.realpath(directory)
        if require_files and not os.path.isdir(directory):
            errors.append(f"Lineuparr plugin directory does not exist: {directory}")
            return [], errors
        rules_by_profile = {rule["profile_name"].casefold(): rule for rule in rules}
        resolved = []
        for mapping in mappings:
            rule = rules_by_profile.get(mapping["profile_name"].casefold())
            if not rule:
                errors.append(
                    f"Lineuparr mapping profile has no name rule: {mapping['profile_name']}."
                )
                continue
            source_path = os.path.realpath(
                os.path.join(directory, mapping["source_name"])
            )
            if os.path.dirname(source_path) != directory:
                errors.append(
                    f"Lineuparr source escapes configured directory: {mapping['source_name']}."
                )
                continue
            if require_files and not os.path.isfile(source_path):
                errors.append(f"Lineuparr source not found: {source_path}")
                continue
            resolved.append({
                **mapping,
                "directory": directory,
                "source_path": source_path,
                "suffixes": rule["suffixes"],
            })
        return resolved, errors

    def _preview_logo_matches(self, settings, logger):
        return self._start_operation(settings, logger, apply_changes=False)

    def _assign_logos(self, settings, logger):
        return self._start_operation(settings, logger, apply_changes=True)

    def _preview_name_changes(self, settings, logger):
        return self._run_name_operation(settings, logger, apply_changes=False)

    def _apply_name_changes(self, settings, logger):
        return self._run_name_operation(settings, logger, apply_changes=True)

    def _name_candidates(self, rules):
        """Return channel candidates and safe profile-rule conflicts."""
        rules_by_profile_id = {rule["profile"].id: rule for rule in rules}
        mapped_profile_ids = set(rules_by_profile_id)
        candidate_ids = set(
            ChannelProfileMembership.objects.filter(
                channel_profile_id__in=mapped_profile_ids, enabled=True
            ).values_list("channel_id", flat=True)
        )
        channels = {
            channel.id: channel
            for channel in Channel.objects.filter(id__in=candidate_ids).select_related(
                "channel_group"
            )
        }
        memberships = ChannelProfileMembership.objects.filter(
            channel_id__in=candidate_ids, enabled=True
        ).select_related("channel_profile")
        profiles_by_channel = {}
        for membership in memberships:
            profiles_by_channel.setdefault(membership.channel_id, []).append(
                membership.channel_profile
            )

        candidates = []
        for channel_id in sorted(candidate_ids):
            channel = channels[channel_id]
            profiles = profiles_by_channel.get(channel_id, [])
            unmapped = [
                profile.name for profile in profiles
                if profile.id not in rules_by_profile_id
            ]
            configured_rules = [
                rules_by_profile_id[profile.id]
                for profile in profiles
                if profile.id in rules_by_profile_id
            ]
            unique_rule_sets = {rule["suffixes"] for rule in configured_rules}
            conflict = ""
            if unmapped:
                conflict = "shared_with_unmapped_profile"
            elif len(unique_rule_sets) > 1:
                conflict = "profile_rule_conflict"
            suffixes = configured_rules[0]["suffixes"] if configured_rules else ()
            candidates.append({
                "channel": channel,
                "profiles": profiles,
                "suffixes": suffixes,
                "conflict": conflict,
            })
        return candidates

    def _run_name_operation(self, settings, logger, apply_changes):
        rules, errors = self._resolve_name_rules(settings)
        if errors:
            return {"status": "error", "message": "\n".join(errors)}
        if not rules:
            return {
                "status": "error",
                "message": "Add at least one Profile Name Rule before normalizing names.",
            }

        candidates = self._name_candidates(rules)
        existing_by_group_name = {}
        for channel_id, group_id, name in Channel.objects.values_list(
            "id", "channel_group_id", "name"
        ):
            existing_by_group_name.setdefault(
                (group_id, str(name).casefold()), set()
            ).add(channel_id)

        rows = []
        planned = []
        target_groups = {}
        for candidate in candidates:
            channel = candidate["channel"]
            normalized, removed = normalize_channel_name(
                channel.name, candidate["suffixes"]
            )
            key = (channel.channel_group_id, normalized.casefold())
            if removed and not candidate["conflict"]:
                target_groups.setdefault(key, []).append(channel.id)
            planned.append((candidate, normalized, removed, key))

        counts = {
            "changed": 0,
            "unchanged": 0,
            "collisions": 0,
            "profile_conflicts": 0,
        }
        changes = []
        for candidate, normalized, removed, key in planned:
            channel = candidate["channel"]
            result = "unchanged"
            if not removed:
                counts["unchanged"] += 1
            elif candidate["conflict"]:
                result = candidate["conflict"]
                counts["profile_conflicts"] += 1
            else:
                other_existing = existing_by_group_name.get(key, set()) - {channel.id}
                same_target = set(target_groups.get(key, [])) - {channel.id}
                if other_existing or same_target:
                    result = "name_collision"
                    counts["collisions"] += 1
                elif apply_changes:
                    result = "changed"
                    changes.append({
                        "channel": channel,
                        "channel_id": channel.id,
                        "group_id": channel.channel_group_id,
                        "original_name": channel.name,
                        "normalized_name": normalized,
                        "removed_suffix": removed,
                    })
                    counts["changed"] += 1
                else:
                    result = "would_change"
                    counts["changed"] += 1
            rows.append({
                "profiles": ", ".join(sorted(profile.name for profile in candidate["profiles"])),
                "channel_id": channel.id,
                "group": channel.channel_group.name if channel.channel_group else "",
                "original_name": channel.name,
                "normalized_name": normalized,
                "removed_suffix": removed,
                "result": result,
            })

        if apply_changes and changes:
            with transaction.atomic():
                for change in changes:
                    channel = change["channel"]
                    channel.name = change["normalized_name"]
                    channel.save(update_fields=["name"])
            self._append_history(changes)
            send_websocket_update("updates", "update", {
                "type": "plugin",
                "plugin": "tidyCH",
                "message": f"Normalized {len(changes)} channel names.",
            })

        os.makedirs(EXPORTS_DIRECTORY, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        mode = "apply" if apply_changes else "preview"
        export_path = os.path.join(
            EXPORTS_DIRECTORY, f"channel_curator_names_{mode}_{timestamp}.csv"
        )
        self._write_name_csv(export_path, rows)
        label = "normalization" if apply_changes else "preview"
        message = (
            f"Name {label} complete: {counts['changed']} "
            f"{'changed' if apply_changes else 'would change'}, "
            f"{counts['unchanged']} unchanged, {counts['collisions']} collisions, "
            f"{counts['profile_conflicts']} profile conflicts. Report: {export_path}"
        )
        self._write_progress({"state": "complete", "message": message})
        logger.info("%s %s", LOG_PREFIX, message)
        return {"status": "ok", "message": message}

    @staticmethod
    def _write_name_csv(path, rows):
        fieldnames = [
            "profiles", "channel_id", "group", "original_name",
            "normalized_name", "removed_suffix", "result",
        ]
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _load_history():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("runs"), list):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "runs": []}

    def _append_history(self, changes):
        history = self._load_history()
        history["runs"].append({
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "rolled_back_at": "",
            "changes": [
                {
                    key: change[key]
                    for key in (
                        "channel_id", "group_id", "original_name",
                        "normalized_name", "removed_suffix",
                    )
                }
                for change in changes
            ],
        })
        history["runs"] = history["runs"][-25:]
        temporary_path = f"{HISTORY_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(history, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary_path, HISTORY_FILE)

    def _rollback_name_changes(self, settings, logger):
        history = self._load_history()
        run = next(
            (item for item in reversed(history["runs"]) if not item.get("rolled_back_at")),
            None,
        )
        if not run:
            return {"status": "error", "message": "No applied name-normalization run is available to roll back."}

        restored = []
        skipped = []
        for change in reversed(run.get("changes", [])):
            if change.get("rollback_status") == "restored":
                continue
            channel = Channel.objects.filter(id=change.get("channel_id")).first()
            if not channel:
                skipped.append(f"channel {change.get('channel_id')} no longer exists")
                continue
            if channel.name != change.get("normalized_name"):
                skipped.append(f"{channel.name}: modified since normalization")
                continue
            collision = Channel.objects.filter(
                channel_group_id=channel.channel_group_id,
                name__iexact=change.get("original_name"),
            ).exclude(id=channel.id).exists()
            if collision:
                skipped.append(f"{channel.name}: original name now collides")
                continue
            restored.append((channel, change.get("original_name")))

        with transaction.atomic():
            for channel, original_name in restored:
                channel.name = original_name
                channel.save(update_fields=["name"])
        restored_ids = {channel.id for channel, _ in restored}
        rollback_time = datetime.now(timezone.utc).isoformat()
        for change in run.get("changes", []):
            if change.get("channel_id") in restored_ids:
                change["rollback_status"] = "restored"
                change["rolled_back_at"] = rollback_time
        pending = [
            change for change in run.get("changes", [])
            if change.get("rollback_status") != "restored"
        ]
        if not pending:
            run["rolled_back_at"] = rollback_time
        else:
            run["rollback_attempted_at"] = rollback_time
        run["rollback_restored"] = sum(
            change.get("rollback_status") == "restored"
            for change in run.get("changes", [])
        )
        run["rollback_skipped"] = skipped
        temporary_path = f"{HISTORY_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(history, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary_path, HISTORY_FILE)
        message = f"Name rollback complete: {len(restored)} restored, {len(skipped)} skipped."
        self._write_progress({"state": "complete", "message": message})
        send_websocket_update("updates", "update", {
            "type": "plugin", "plugin": "tidyCH", "message": message
        })
        return {"status": "ok", "message": message}

    def _generate_lineuparr_lineups(self, settings, logger):
        rules, rule_errors = self._resolve_name_rules(settings)
        mappings, mapping_errors = self._resolve_lineuparr_mappings(
            settings, rules, require_files=True
        )
        errors = rule_errors + mapping_errors
        if errors:
            return {"status": "error", "message": "\n".join(errors)}
        if not mappings:
            return {"status": "error", "message": "Add at least one Lineuparr lineup mapping."}

        generated = []
        all_rows = []
        prepared = []
        collision_count = 0
        for mapping in mappings:
            with open(mapping["source_path"], "r", encoding="utf-8") as handle:
                source_payload = json.load(handle)
            output_payload, changes, collisions = build_normalized_lineup(
                source_payload, mapping["suffixes"]
            )
            for row in changes + collisions:
                all_rows.append({"profile": mapping["profile_name"], **row})
            collision_count += len(collisions)
            prepared.append((mapping, output_payload, len(changes)))

        os.makedirs(EXPORTS_DIRECTORY, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(
            EXPORTS_DIRECTORY, f"channel_curator_lineuparr_{timestamp}.csv"
        )
        fieldnames = [
            "profile", "category", "original_name", "normalized_name",
            "removed_suffix", "result",
        ]
        with open(export_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        if collision_count:
            return {
                "status": "error",
                "message": (
                    f"No lineups generated: {collision_count} normalization collisions. "
                    f"Report: {export_path}"
                ),
            }
        for mapping, output_payload, change_count in prepared:
            output_path, backup_path = write_lineup(
                mapping["directory"], mapping["output_name"], output_payload
            )
            generated.append(
                f"{output_path} ({change_count} names normalized)"
                + (f", previous output backed up to {backup_path}" if backup_path else "")
            )
        message = (
            f"Generated {len(generated)} Lineuparr-compatible lineup(s).\n"
            + "\n".join(generated)
            + f"\nReport: {export_path}"
        )
        self._write_progress({"state": "complete", "message": message})
        return {"status": "ok", "message": message}

    def _start_operation(self, settings, logger, apply_changes):
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return {"status": "error", "message": "A tidyCH operation is already running."}
            if os.path.exists(LOCK_FILE):
                try:
                    age = time.time() - os.path.getmtime(LOCK_FILE)
                except OSError:
                    age = 0
                if age < 1800:
                    return {"status": "error", "message": "A tidyCH operation is already running in another worker."}
                try:
                    os.remove(LOCK_FILE)
                except OSError:
                    pass
            try:
                descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, str(os.getpid()).encode())
                os.close(descriptor)
            except FileExistsError:
                return {"status": "error", "message": "A tidyCH operation is already running."}
            try:
                os.remove(CANCEL_FILE)
            except OSError:
                pass
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_operation_background,
                args=(dict(settings), logger, apply_changes),
                daemon=True,
            )
            self._thread.start()
        label = "assignment" if apply_changes else "preview"
        return {
            "status": "ok",
            "message": f"Logo {label} started. Click Status to follow progress.",
            "background": True,
        }

    def _run_operation_background(self, settings, logger, apply_changes):
        try:
            result = self._run_operation(settings, logger, apply_changes)
            message = result.get("message", "Logo operation finished.")
            state = "complete" if result.get("status") == "ok" else "error"
            self._write_progress({"state": state, "message": message})
            send_websocket_update("updates", "update", {
                "type": "plugin", "plugin": "tidyCH", "message": message
            })
        except Exception as exc:
            logger.exception("%s Background operation failed", LOG_PREFIX)
            message = f"Logo operation failed: {exc}"
            self._write_progress({"state": "error", "message": message})
            send_websocket_update("updates", "update", {
                "type": "plugin", "plugin": "tidyCH", "message": message
            })
        finally:
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass

    def _run_operation(self, settings, logger, apply_changes):
        configuration, errors = self._resolve_configuration(settings, fetch_files=True)
        if errors:
            return {"status": "error", "message": "\n".join(errors)}

        candidates_by_channel = {}
        for mapping in configuration["mappings"]:
            memberships = ChannelProfileMembership.objects.filter(
                channel_profile=mapping["profile"], enabled=True
            ).select_related("channel__channel_group", "channel__logo")
            for membership in memberships:
                candidates_by_channel.setdefault(membership.channel_id, []).append(
                    (membership.channel, mapping)
                )

        total = len(candidates_by_channel)
        self._write_progress({
            "state": "running", "processed": 0, "total": total,
            "message": f"Preparing {total} unique channels."
        })
        os.makedirs(EXPORTS_DIRECTORY, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        mode = "apply" if apply_changes else "preview"
        export_path = os.path.join(
            EXPORTS_DIRECTORY, f"channel_curator_logos_{mode}_{timestamp}.csv"
        )

        existing_logos = {logo.url: logo for logo in Logo.objects.all()}
        rows = []
        channels_to_update = []
        counts = {
            "matched": 0, "changed": 0, "already_current": 0,
            "would_change": 0, "existing_preserved": 0,
            "unmatched": 0, "conflicts": 0,
        }

        for processed, entries in enumerate(candidates_by_channel.values(), start=1):
            if self._stop_event.is_set() or os.path.exists(CANCEL_FILE):
                message = f"Cancelled after {processed - 1} of {total} channels."
                self._write_csv(export_path, rows)
                return {"status": "ok", "message": f"{message} Report: {export_path}"}

            channel = entries[0][0]
            unique_sources = {
                (entry[1]["country"], entry[1]["directory"]) for entry in entries
            }
            profiles = ", ".join(sorted({entry[1]["profile"].name for entry in entries}))
            matched_file = ""
            score = 0
            target_url = ""
            country = ""

            if len(unique_sources) > 1:
                result = "profile_conflict"
                counts["conflicts"] += 1
            elif (
                apply_changes
                and channel.logo_id is not None
                and not configuration["replace_existing"]
            ):
                result = "existing_preserved"
                counts["existing_preserved"] += 1
            else:
                mapping = entries[0][1]
                country = self._channel_country(
                    channel, mapping["country"], mapping["files"]
                )
                match_name = re.sub(
                    r"^[A-Z]{2}\s*[|:]\s*", "", channel.name,
                    flags=re.IGNORECASE,
                )
                matched_file, score = match_channel(
                    match_name,
                    mapping["files"],
                    country,
                    configuration["threshold"],
                )
                if not matched_file:
                    result = "no_match"
                    counts["unmatched"] += 1
                else:
                    counts["matched"] += 1
                    target_url = build_raw_url(
                        configuration["repository"], configuration["branch"],
                        mapping["directory"], matched_file,
                    )
                    current_url = channel.logo.url if channel.logo else ""
                    if current_url == target_url:
                        result = "already_current"
                        counts["already_current"] += 1
                    elif channel.logo_id is not None and not configuration["replace_existing"]:
                        result = "existing_preserved"
                        counts["existing_preserved"] += 1
                    elif apply_changes:
                        logo = existing_logos.get(target_url)
                        if not logo:
                            logo = Logo.objects.create(name=channel.name, url=target_url)
                            existing_logos[target_url] = logo
                        channel.logo = logo
                        channels_to_update.append(channel)
                        result = "changed"
                        counts["changed"] += 1
                    else:
                        result = "would_change"
                        counts["would_change"] += 1

            rows.append({
                "profiles": profiles,
                "channel": channel.name,
                "group": channel.channel_group.name if channel.channel_group else "",
                "current_logo": channel.logo.url if channel.logo else "",
                "country": country,
                "matched_file": matched_file or "",
                "score": score,
                "target_logo": target_url,
                "result": result,
            })
            if processed % 25 == 0 or processed == total:
                self._write_progress({
                    "state": "running", "processed": processed, "total": total,
                    "message": f"Processed {processed} of {total} channels."
                })

        if apply_changes and channels_to_update:
            with transaction.atomic():
                Channel.objects.bulk_update(channels_to_update, ["logo_id"])
        self._write_csv(export_path, rows)
        action_word = "assignment" if apply_changes else "preview"
        change_summary = (
            f"{counts['changed']} changed"
            if apply_changes else f"{counts['would_change']} would change"
        )
        message = (
            f"Logo {action_word} complete: {counts['matched']} matched, "
            f"{change_summary}, {counts['already_current']} already current, "
            f"{counts['existing_preserved']} existing preserved, "
            f"{counts['unmatched']} unmatched, {counts['conflicts']} profile conflicts. "
            f"Report: {export_path}"
        )
        logger.info("%s %s", LOG_PREFIX, message)
        return {"status": "ok", "message": message}

    @staticmethod
    def _channel_country(channel, default_country, filenames):
        """Allow a two-letter channel or group prefix to override its profile."""
        group_name = channel.channel_group.name if channel.channel_group else ""
        for value in (channel.name, group_name):
            match = re.match(r"^([A-Z]{2})\s*[|:]", value, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).lower()
            suffix_pattern = re.compile(
                rf"[-._]{re.escape(candidate)}\.(png|svg|jpg|jpeg|gif|webp)$",
                flags=re.IGNORECASE,
            )
            if any(suffix_pattern.search(filename) for filename in filenames):
                return candidate
        return default_country

    @staticmethod
    def _write_csv(path, rows):
        fieldnames = [
            "profiles", "channel", "group", "current_logo",
            "country", "matched_file", "score", "target_logo", "result",
        ]
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_progress(payload):
        temporary_path = f"{PROGRESS_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump({**payload, "updated_at": time.time()}, handle)
        os.replace(temporary_path, PROGRESS_FILE)

    def _plugin_status(self, settings, logger):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as handle:
                progress = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"status": "ok", "message": "No tidyCH operation has run yet."}
        if progress.get("state") == "running":
            return {
                "status": "ok",
                "message": (
                    f"{progress.get('message', 'Running')} "
                    f"({progress.get('processed', 0)}/{progress.get('total', 0)})"
                ),
            }
        return {"status": "ok", "message": progress.get("message", "No status available.")}

    def _cancel_operation(self, settings, logger):
        self._stop_event.set()
        with open(CANCEL_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
        return {"status": "ok", "message": "Cancellation requested."}
