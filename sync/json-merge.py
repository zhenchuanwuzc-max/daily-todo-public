#!/usr/bin/env python3
"""Three-way merge driver for todos.json and its follow-up configuration.

Field values are compared with the common ancestor before considering clocks.
Only genuine same-field conflicts use _field_updated / updated_at clocks.
The driver never repairs malformed JSON or drops an unreadable input silently.
"""
import copy
from datetime import datetime, timezone
import json
import os
import sys
import tempfile

CONFIG_COLLECTIONS = ("people", "weekly_report_groups")
COLLECTIONS = ("tasks", "projects", "milestones", *CONFIG_COLLECTIONS)
MISSING = object()
MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(_value):
    raise ValueError("non-finite JSON number")


def validate(data, *, allow_missing_tasks=False):
    """Reject broken collection shapes instead of quietly losing records."""
    if not isinstance(data, dict):
        raise ValueError("document must be an object")
    if not allow_missing_tasks and "tasks" not in data:
        raise ValueError("tasks collection is required")
    for name in COLLECTIONS:
        records = data.get(name, [])
        if not isinstance(records, list):
            raise ValueError("collection must be a list")
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
            identity = record.get("id")
            if not isinstance(identity, str) or not identity.strip() or identity in seen:
                raise ValueError("record IDs must be unique non-empty strings")
            seen.add(identity)
            stamps = record.get("_field_updated", {})
            if not isinstance(stamps, dict) or any(not isinstance(v, str) for v in stamps.values()):
                raise ValueError("field clocks must be a string map")
            if "done" in record and not isinstance(record["done"], bool):
                raise ValueError("done must be a boolean")
            if record.get("deleted_at") is not None and not isinstance(record["deleted_at"], str):
                raise ValueError("deleted_at must be a timestamp or null")
            string_fields = {"projects": ("dingtalk_group_id",),
                             "people": ("name", "employee_id"),
                             "weekly_report_groups": ("group_id",)}.get(name, ())
            if any(key in record and not isinstance(record[key], str) for key in string_fields):
                raise ValueError("follow-up configuration fields must be strings")
    for key in ("revision", "schema_version"):
        if key in data and (isinstance(data[key], bool) or not isinstance(data[key], int) or data[key] < 0):
            raise ValueError("invalid version metadata")
    return data


def load(path, *, empty_base=False):
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    # Git uses an empty ancestor for an add/add conflict on a newly created file.
    if empty_base and not raw.strip():
        return {}
    return validate(json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_bad_constant),
                    allow_missing_tasks=empty_base)


def _time(value):
    if not isinstance(value, str) or not value:
        return MIN_TIME
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Legacy created/done_at were local wall time on Ocean's Beijing devices.
        if parsed.tzinfo is None:
            from datetime import timedelta
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return MIN_TIME


def _clock(record, keys):
    stamps = record.get("_field_updated", {})
    specific = [_time(stamps.get(key)) for key in keys]
    stamp = max(specific, default=MIN_TIME)
    if stamp != MIN_TIME:
        return stamp
    for key in ("updated_at", "done_at", "created", "created_at", "updated"):
        stamp = _time(record.get(key))
        if stamp != MIN_TIME:
            return stamp
    return MIN_TIME


def _canonical(value):
    if value is MISSING:
        return "0:missing"
    if isinstance(value, tuple):
        return "1:" + "|".join(_canonical(part) for part in value)
    return "2:" + json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _touched(record, base_record, keys):
    current = record.get("_field_updated", {})
    previous = base_record.get("_field_updated", {})
    return any(key in current and current[key] != previous.get(key)
               and _time(current[key]) != MIN_TIME for key in keys)


def _choose(base, ours, theirs, our_record, their_record, keys, base_record=None):
    """Return selected value and source, independent of merge argument order."""
    if ours == theirs:
        return ours, "both"
    base_record = base_record or {}
    our_changed = ours != base or _touched(our_record, base_record, keys)
    their_changed = theirs != base or _touched(their_record, base_record, keys)
    # A toggle/reassignment may return to the ancestor's value. Its fresh field
    # clock still represents an explicit edit and must compete with stale work.
    if not our_changed:
        return theirs, "theirs"
    if not their_changed:
        return ours, "ours"
    our_rank = (_clock(our_record, keys), _canonical(ours))
    their_rank = (_clock(their_record, keys), _canonical(theirs))
    return (ours, "ours") if our_rank >= their_rank else (theirs, "theirs")


def _stamp_for(record, key):
    return record.get("_field_updated", {}).get(key, MISSING)


def _newer_stamp(ours, theirs):
    return max((ours, theirs), key=lambda value: (_time(value), _canonical(value)))


def merge_record(base, ours, theirs, *, collection=None):
    base = base or {}
    result, stamps = {}, {}
    all_keys = set(base) | set(ours) | set(theirs)
    groups = [("project_id", "milestone_id"), ("done", "done_at")]
    if collection == "people":
        # A name and employee number identify one recipient. Never combine a
        # name from one device with an employee number from another device.
        groups.append(("name", "employee_id"))
    grouped = {key for group in groups for key in group}
    groups += [(key,) for key in sorted(all_keys - grouped - {"_field_updated", "updated_at"})]
    for keys in groups:
        if not any(key in all_keys for key in keys):
            continue
        bvalue = tuple(base.get(key, MISSING) for key in keys)
        ovalue = tuple(ours.get(key, MISSING) for key in keys)
        tvalue = tuple(theirs.get(key, MISSING) for key in keys)
        chosen, source = _choose(bvalue, ovalue, tvalue, ours, theirs, keys, base)
        for key, value in zip(keys, chosen):
            if value is not MISSING:
                result[key] = copy.deepcopy(value)
            if source == "both":
                stamp = _newer_stamp(_stamp_for(ours, key), _stamp_for(theirs, key))
            else:
                stamp = _stamp_for(ours if source == "ours" else theirs, key)
            if stamp is not MISSING:
                stamps[key] = stamp
    # Clocks for previously removed fields are retained as well.
    for key in set(base.get("_field_updated", {})) | set(ours.get("_field_updated", {})) | set(theirs.get("_field_updated", {})):
        if key not in all_keys:
            stamp = _newer_stamp(_stamp_for(ours, key), _stamp_for(theirs, key))
            if stamp is not MISSING:
                stamps[key] = stamp
    if stamps:
        result["_field_updated"] = stamps
    if "updated_at" in all_keys:
        result["updated_at"] = _newer_stamp(ours.get("updated_at", MISSING), theirs.get("updated_at", MISSING))
        if result["updated_at"] is MISSING:
            del result["updated_at"]
    if result.get("done") is False and "done_at" in result:
        result["done_at"] = None
    return result


def _semantic(record):
    return {key: value for key, value in record.items() if key not in ("_field_updated", "updated_at")}


def _merge_collection(name, ours, theirs, base):
    # Older app snapshots do not know the configuration collections. Their
    # absence is not an explicit deletion. Real group removals use tombstones.
    fallback = base.get(name, []) if name in CONFIG_COLLECTIONS else []
    records = [data.get(name, fallback) for data in (ours, theirs, base)]
    maps = [{record["id"]: record for record in items} for items in records]
    our_map, their_map, base_map = maps
    # Preserve the established remote-first order; sort_order is the UI ordering key.
    order = list(dict.fromkeys([record["id"] for record in records[1]] + [record["id"] for record in records[0]]))
    merged = []
    for identity in order:
        our, their, old = our_map.get(identity), their_map.get(identity), base_map.get(identity)
        if our is not None and their is not None:
            merged.append(merge_record(old, our, their, collection=name))
        else:
            remaining = our if our is not None else their
            # A legacy physical deletion wins over an unchanged ancestor. An edit
            # versus deletion retains the edited record to avoid losing new work.
            if old is None or _semantic(remaining) != _semantic(old) or _touched(remaining, old, set(remaining) | set(old)):
                merged.append(copy.deepcopy(remaining))
    return merged


def union(ours, theirs, base):
    validate(ours)
    validate(theirs)
    validate(base, allow_missing_tasks=True)
    result = {}
    for key in sorted((set(base) | set(ours) | set(theirs)) - set(COLLECTIONS) - {"revision", "schema_version"}):
        chosen, _source = _choose(base.get(key, MISSING), ours.get(key, MISSING), theirs.get(key, MISSING), ours, theirs, (key,), base)
        if chosen is not MISSING:
            result[key] = copy.deepcopy(chosen)
    for name in COLLECTIONS:
        if any(name in document for document in (base, ours, theirs)):
            result[name] = _merge_collection(name, ours, theirs, base)
    result["revision"] = max(document.get("revision", 0) for document in (base, ours, theirs)) + 1
    if any("schema_version" in document for document in (base, ours, theirs)):
        result["schema_version"] = max(document.get("schema_version", 0) for document in (base, ours, theirs))
    return validate(result)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("todos merge failed: expected ancestor, ours, theirs", file=sys.stderr)
        return 1
    ancestor, ours_path, theirs_path = args
    temp_path = None
    try:
        base = load(ancestor, empty_base=True)
        result = union(load(ours_path), load(theirs_path), base)
        # Replace ours only after every input and the complete result are valid.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(os.path.abspath(ours_path)), delete=False) as handle:
            temp_path = handle.name
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temp_path, ours_path)
        return 0
    except (OSError, ValueError, TypeError):
        # Avoid putting private record contents or filesystem paths into stderr.
        print("todos merge failed: invalid input or write failure; original retained", file=sys.stderr)
        return 1
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


if __name__ == "__main__":
    raise SystemExit(main())
