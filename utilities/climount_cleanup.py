"""
cli_mount DB cleanup — removes entries for a specific debrid provider
from entries.db and items.db using infohash matching.

Adapted from climount_remove_rd.py for use inside cli_debrid.
"""

import struct
import os
import shutil
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

MAGIC = b'HYBR'
HEADER_SIZE = 16


# ---------------------------------------------------------------------------
# HYBR log parser
# ---------------------------------------------------------------------------

def read_hybr_records(path: str):
    records = []
    file_size = os.path.getsize(path)
    with open(path, 'rb') as f:
        magic = f.read(4)
        if magic != MAGIC:
            raise ValueError(f"Not a HYBR file: {path}")
        version = struct.unpack('<I', f.read(4))[0]
        f.read(8)
        pos = HEADER_SIZE
        while pos < file_size:
            try:
                f.seek(pos)
                key_len = struct.unpack('<I', f.read(4))[0]
                if key_len > 1024 * 1024:
                    break
                key = f.read(key_len).decode('utf-8', errors='replace')
                pos += 4 + key_len

                f.seek(pos); val_len = struct.unpack('<I', f.read(4))[0]
                pos += 4
                value_start = pos
                f.seek(pos); value_bytes = f.read(val_len)
                pos += val_len

                f.seek(pos); flags = f.read(1)[0]
                flags_pos = pos
                deleted = bool(flags & 1)
                pos += 1

                f.seek(pos); cat_len = struct.unpack('<H', f.read(2))[0]; pos += 2
                f.seek(pos); category = f.read(cat_len).decode('utf-8', errors='replace'); pos += cat_len

                f.seek(pos); prov_len = struct.unpack('<H', f.read(2))[0]; pos += 2
                f.seek(pos); provider = f.read(prov_len).decode('utf-8', errors='replace'); pos += prov_len

                f.seek(pos); status_len = struct.unpack('<H', f.read(2))[0]; pos += 2
                f.seek(pos); status = f.read(status_len).decode('utf-8', errors='replace'); pos += status_len

                f.seek(pos); name_len = struct.unpack('<H', f.read(2))[0]; pos += 2
                f.seek(pos); name = f.read(name_len).decode('utf-8', errors='replace'); pos += name_len

                f.seek(pos); total_size = struct.unpack('<Q', f.read(8))[0]; pos += 8

                protocol = ''
                added_on = 0
                if version >= 3:
                    f.seek(pos); proto_len = struct.unpack('<H', f.read(2))[0]; pos += 2
                    f.seek(pos); protocol = f.read(proto_len).decode('utf-8', errors='replace'); pos += proto_len
                    f.seek(pos); added_on = struct.unpack('<Q', f.read(8))[0]; pos += 8

                records.append({
                    'key': key, 'name': name, 'flags_pos': flags_pos,
                    'flags': flags, 'deleted': deleted,
                    'provider': provider, 'category': category,
                    'status': status, 'protocol': protocol,
                    'total_size': total_size, 'added_on': added_on,
                    'value_start': value_start, 'value_bytes': value_bytes,
                })
            except Exception as e:
                logger.warning(f"Error at pos {pos} in {os.path.basename(path)}: {e}")
                break
    return version, records


def mark_deleted(path: str, records: list) -> int:
    count = 0
    with open(path, 'r+b') as f:
        for r in records:
            f.seek(r['flags_pos'])
            f.write(bytes([r['flags'] | 1]))
            count += 1
        f.flush()
    return count


def append_record(path, key, value_bytes, category, provider, status, name,
                  total_size, protocol, added_on):
    key_b = key.encode('utf-8')
    cat_b = category.encode('utf-8')
    prov_b = provider.encode('utf-8')
    status_b = status.encode('utf-8')
    name_b = name.encode('utf-8')
    proto_b = protocol.encode('utf-8')

    buf = b''
    buf += struct.pack('<I', len(key_b)) + key_b
    buf += struct.pack('<I', len(value_bytes)) + value_bytes
    buf += bytes([0])
    buf += struct.pack('<H', len(cat_b)) + cat_b
    buf += struct.pack('<H', len(prov_b)) + prov_b
    buf += struct.pack('<H', len(status_b)) + status_b
    buf += struct.pack('<H', len(name_b)) + name_b
    buf += struct.pack('<Q', total_size)
    buf += struct.pack('<H', len(proto_b)) + proto_b
    buf += struct.pack('<Q', added_on)

    with open(path, 'ab') as f:
        f.write(buf)
        f.flush()


def backup(path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bp = path + f'.backup_{ts}'
    shutil.copy2(path, bp)
    return bp


# ---------------------------------------------------------------------------
# Protobuf helpers
# ---------------------------------------------------------------------------

def pb_read_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos


def pb_read_field(data, pos):
    if pos >= len(data): return None, None, None, pos
    tag, pos = pb_read_varint(data, pos)
    fn, wt = tag >> 3, tag & 0x7
    if wt == 0: val, pos = pb_read_varint(data, pos)
    elif wt == 2:
        l, pos = pb_read_varint(data, pos); val = data[pos:pos+l]; pos += l
    elif wt == 1: val = data[pos:pos+8]; pos += 8
    elif wt == 5: val = data[pos:pos+4]; pos += 4
    else: raise ValueError(f"Unknown wire type {wt}")
    return fn, wt, val, pos


def get_file_infohash(file_bytes):
    pos = 0
    while pos < len(file_bytes):
        fn, wt, val, pos = pb_read_field(file_bytes, pos)
        if fn is None: break
        if fn == 9 and wt == 2:
            return val.decode('utf-8', errors='replace')
    return ''


def rebuild_without_provider_files(value_bytes, provider_hashes):
    kept = b''
    kept_count = removed_count = 0
    pos = 0
    while pos < len(value_bytes):
        field_start = pos
        fn, wt, val, pos = pb_read_field(value_bytes, pos)
        if fn is None: break
        raw = value_bytes[field_start:pos]
        if fn == 2 and wt == 2:
            mk, fv = b'', b''
            mpos = 0
            while mpos < len(val):
                mfn, mwt, mv, mpos = pb_read_field(val, mpos)
                if mfn is None: break
                if mfn == 1 and mwt == 2: mk = mv
                elif mfn == 2 and mwt == 2: fv = mv
            infohash = get_file_infohash(fv)
            if infohash and infohash.lower() in provider_hashes:
                removed_count += 1
                continue
            kept_count += 1
            kept += raw
        elif fn == 3 and wt == 0:
            pass
        else:
            kept += raw
    return kept, kept_count, removed_count


def parse_item_files(value_bytes):
    files = []
    pos = 0
    while pos < len(value_bytes):
        fn, wt, val, pos = pb_read_field(value_bytes, pos)
        if fn is None: break
        if fn == 2 and wt == 2:
            mk, fv = b'', b''
            mpos = 0
            while mpos < len(val):
                mfn, mwt, mv, mpos = pb_read_field(val, mpos)
                if mfn is None: break
                if mfn == 1 and mwt == 2: mk = mv
                elif mfn == 2 and mwt == 2: fv = mv
            infohash = get_file_infohash(fv)
            files.append((mk, fv, infohash))
    return files


# ---------------------------------------------------------------------------
# Main callable
# ---------------------------------------------------------------------------

def run_cleanup(db_dir: str, provider: str, dry_run: bool = True) -> Dict[str, Any]:
    """
    Run the cli_mount cleanup for the given provider.
    Returns a dict with status, counts, samples, and log lines.
    """
    lines = []
    result = {
        'success': False,
        'dry_run': dry_run,
        'provider': provider,
        'lines': lines,
        'entries_to_delete': 0,
        'items_full_delete': 0,
        'items_partial': 0,
        'items_untouched': 0,
        'entries_deleted': 0,
        'items_deleted': 0,
        'items_updated': 0,
        'sample_entries': [],
        'sample_items': [],
    }

    entries_path = os.path.join(db_dir, 'entries.db')
    items_path = os.path.join(db_dir, 'items.db')

    for p in [entries_path, items_path]:
        if not os.path.exists(p):
            result['error'] = f"File not found: {p}"
            lines.append(f"ERROR: File not found: {p}")
            return result

    lines.append(f"DB directory: {db_dir}")
    lines.append(f"Provider to remove: {provider}")
    lines.append(f"Dry run: {dry_run}")
    lines.append("")

    # Parse entries.db
    lines.append("Parsing entries.db...")
    _, entries_records = read_hybr_records(entries_path)
    provider_entries = [r for r in entries_records if r['provider'] == provider and not r['deleted']]
    nzb_kept = [r for r in entries_records if r['protocol'] == 'nzb' and not r['deleted']]
    provider_hashes = {r['key'].lower() for r in provider_entries if r['key']}

    lines.append(f"  Total records: {len(entries_records)}")
    lines.append(f"  '{provider}' to delete: {len(provider_entries)}")
    lines.append(f"  NZB records (kept): {len(nzb_kept)}")
    lines.append(f"  Provider infohashes: {len(provider_hashes)}")

    result['entries_to_delete'] = len(provider_entries)
    result['sample_entries'] = [r['name'] or r['key'][:60] for r in provider_entries[:5]]

    # Parse items.db
    lines.append("")
    lines.append("Parsing items.db (infohash-based)...")
    _, items_records = read_hybr_records(items_path)

    items_full = []
    items_partial_list = []
    items_skip = 0

    for r in items_records:
        if r['deleted']:
            continue
        try:
            files = parse_item_files(r['value_bytes'])
        except Exception as e:
            lines.append(f"  WARN: Could not parse {r['key'][:60]}: {e}")
            continue

        if not files:
            continue

        provider_files = [f for f in files if f[2].lower() in provider_hashes]
        non_provider_files = [f for f in files if f[2].lower() not in provider_hashes]

        if not provider_files:
            items_skip += 1
        elif not non_provider_files:
            items_full.append(r)
        else:
            try:
                new_bytes, kept, removed = rebuild_without_provider_files(r['value_bytes'], provider_hashes)
                items_partial_list.append((r, new_bytes))
            except Exception as e:
                lines.append(f"  WARN: Could not rebuild {r['key'][:60]}: {e}")
                items_full.append(r)

    lines.append(f"  Total records: {len(items_records)}")
    lines.append(f"  Folders to fully delete (all {provider}): {len(items_full)}")
    lines.append(f"  Folders with mixed files ({provider} removed, others kept): {len(items_partial_list)}")
    lines.append(f"  Folders with no {provider} files (untouched): {items_skip}")

    result['items_full_delete'] = len(items_full)
    result['items_partial'] = len(items_partial_list)
    result['items_untouched'] = items_skip
    result['sample_items'] = [r['key'][:70] for r in items_full[:5]]

    if items_full:
        lines.append("")
        lines.append("Sample folders to fully delete:")
        for r in items_full[:5]:
            lines.append(f"  {r['key'][:70]}")
        if len(items_full) > 5:
            lines.append(f"  ... and {len(items_full) - 5} more")

    if items_partial_list:
        lines.append("")
        lines.append("Sample mixed folders (provider removed, others kept):")
        for r, _ in items_partial_list[:5]:
            lines.append(f"  {r['key'][:70]}")

    if dry_run:
        lines.append("")
        lines.append("DRY RUN — no changes made.")
        result['success'] = True
        return result

    if not provider_entries and not items_full and not items_partial_list:
        lines.append("")
        lines.append("Nothing to delete.")
        result['success'] = True
        return result

    # Create backups
    lines.append("")
    lines.append("Creating backups...")
    for p in [entries_path, items_path]:
        bp = backup(p)
        lines.append(f"  {bp}")

    # Clean entries.db
    lines.append("")
    lines.append("Cleaning entries.db...")
    n1 = mark_deleted(entries_path, provider_entries)
    lines.append(f"  Deleted {n1} entries")
    result['entries_deleted'] = n1

    # Clean items.db — full deletes
    lines.append("Cleaning items.db (full deletes)...")
    n2 = mark_deleted(items_path, items_full)
    lines.append(f"  Fully deleted {n2} folders")
    result['items_deleted'] = n2

    # Clean items.db — partial
    lines.append("Cleaning items.db (partial — marking old deleted + appending new)...")
    n3 = n3_fail = 0
    for r, new_bytes in items_partial_list:
        try:
            mark_deleted(items_path, [r])
            append_record(
                items_path,
                key=r['key'], value_bytes=new_bytes,
                category=r['category'], provider=r['provider'],
                status=r['status'], name=r['name'],
                total_size=r['total_size'], protocol=r['protocol'],
                added_on=r['added_on'],
            )
            n3 += 1
        except Exception as e:
            lines.append(f"  WARN: {r['key'][:60]}: {e}")
            n3_fail += 1

    lines.append(f"  Partially updated {n3} folders")
    result['items_updated'] = n3
    if n3_fail:
        lines.append(f"  {n3_fail} failures")

    lines.append("")
    lines.append("Done.")
    lines.append(f"  entries.db:  {n1} deleted")
    lines.append(f"  items.db:    {n2} fully deleted + {n3} partially updated")
    lines.append("Restart cli_mount — provider files will no longer appear in the mount.")

    result['success'] = True
    return result


def get_climount_providers(data_path: str) -> List[str]:
    """Read unique provider names directly from entries.db."""
    entries_path = os.path.join(data_path, 'entries.db')
    if not os.path.exists(entries_path):
        return []
    try:
        _, records = read_hybr_records(entries_path)
        providers = sorted({r['provider'] for r in records if r['provider'] and not r['deleted']})
        return providers
    except Exception as e:
        logger.warning(f"Could not read providers from entries.db: {e}")
        return []
