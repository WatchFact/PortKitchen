"""Archive handling, partition selection and logical-super assembly.

A ported userspace image is *not* proof of device compatibility. The tool never
flashes devices, disables AVB, or silently substitutes boot/vendor firmware.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from . import __version__

PARTITION_RE = re.compile(r"^(system|system_ext|product|vendor|vendor_dlkm|odm|odm_dlkm|system_dlkm|vendor_boot|init_boot|boot|dtbo|vbmeta|vbmeta_system|recovery|super)(?:_([ab]))?\.img(?:\.lz4)?$", re.I)
DEFAULT_DONOR = ("system", "system_ext", "product")
MAX_ARCHIVE_MEMBERS = 20000
MAX_ENTRY_BYTES = 16 * 1024**3
MAX_TOTAL_BYTES = 60 * 1024**3
ALIGN = 4096

class KitchenError(RuntimeError):
    """Validation or processing error with a human-readable explanation."""


def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise KitchenError(f"Required tool not found: {name}. Install the AOSP utility and retry.")
    return path


def run(*args: object) -> None:
    cmd = [str(a) for a in args]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        raise KitchenError(f"{' '.join(cmd[:2])} failed: {exc.stderr.decode(errors='replace')[-1000:]}") from exc


def hash_file(path: Path) -> str:
    d = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            d.update(block)
    return d.hexdigest()


def _safe_member(name: str) -> bool:
    p = Path(name.replace('\\', '/'))
    return not p.is_absolute() and not any(part in ('..', '') for part in p.parts) and not re.match(r'^[A-Za-z]:', name)


def _candidate(name: str, depth: int) -> bool:
    basename = Path(name).name.lower()
    return bool(PARTITION_RE.fullmatch(basename)) or basename in ("firmware.json",) or (depth < 2 and (basename.endswith(('.zip', '.tar', '.tar.md5')) or basename.startswith('ap_') and basename.endswith('.md5')))


def _copy_limited(source, dest: Path, limit: int) -> None:
    written = 0
    with dest.open('wb') as out:
        while block := source.read(min(4 * 1024 * 1024, limit + 1 - written)):
            written += len(block)
            if written > limit:
                raise KitchenError(f"Archive entry too large: {dest.name}")
            out.write(block)


def _extract_archive(source: Path, out: Path, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_TOTAL_BYTES]
    if depth > 2:
        raise KitchenError('Too many nested archives')
    if source.is_file() and (PARTITION_RE.fullmatch(source.name.lower()) or source.name == 'firmware.json'):
        _place_file(source, out, depth, budget)
        return
    if source.is_dir():
        for path in source.rglob('*'):
            if path.is_symlink():
                raise KitchenError('Firmware directories must not contain symlinks')
            if path.is_file() and _candidate(path.name, depth):
                _place_file(path, out, depth, budget)
        return
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as arc:
            members = arc.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise KitchenError('Archive contains too many entries')
            for member in members:
                if not _safe_member(member.filename):
                    raise KitchenError('Unsafe ZIP member path')
                if member.is_dir() or not _candidate(member.filename, depth):
                    continue
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise KitchenError('ZIP symlinks are not accepted')
                _accept_entry(member.file_size, budget)
                with arc.open(member) as inp:
                    temp = _temporary(out, Path(member.filename).name)
                    _copy_limited(inp, temp, member.file_size)
                _place_file(temp, out, depth, budget, already_counted=True)
                temp.unlink(missing_ok=True)
                temp.parent.rmdir()
        return
    try:
        arc = tarfile.open(source, 'r:*')
    except tarfile.TarError as exc:
        raise KitchenError(f"Unsupported firmware container: {source.name}") from exc
    with arc:
        for index, member in enumerate(arc):
            if index >= MAX_ARCHIVE_MEMBERS:
                raise KitchenError('Archive contains too many entries')
            if not _safe_member(member.name):
                raise KitchenError('Unsafe TAR member path')
            if member.issym() or member.islnk():
                raise KitchenError('TAR symlinks are not accepted')
            if not member.isfile() or not _candidate(member.name, depth):
                continue
            _accept_entry(member.size, budget)
            temp = _temporary(out, Path(member.name).name)
            with arc.extractfile(member) as inp:
                _copy_limited(inp, temp, member.size)
            _place_file(temp, out, depth, budget, already_counted=True)
            temp.unlink(missing_ok=True)
            temp.parent.rmdir()


def _temporary(out: Path, name: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix='.pk-', dir=out))
    return directory / name


def _accept_entry(size: int, budget: list[int]) -> None:
    if size < 0 or size > MAX_ENTRY_BYTES or size > budget[0]:
        raise KitchenError('Firmware extraction exceeds configured size limit')
    budget[0] -= size


def _place_file(path: Path, out: Path, depth: int, budget: list[int], already_counted: bool = False) -> None:
    name = path.name.lower()
    if not already_counted:
        _accept_entry(path.stat().st_size, budget)
    if depth < 2 and name.endswith(('.zip', '.tar', '.tar.md5')):
        _extract_archive(path, out, depth + 1, budget)
    elif name == 'firmware.json':
        dest = out / name
        if dest.exists() and dest.read_bytes() != path.read_bytes():
            raise KitchenError('Conflicting firmware.json manifests')
        shutil.copyfile(path, dest)
    elif PARTITION_RE.fullmatch(name):
        dest = out / name
        if dest.exists():
            raise KitchenError(f'Duplicate partition image: {name}')
        if name.endswith('.lz4'):
            if dest.with_suffix('').exists():
                raise KitchenError(f'Duplicate partition image: {dest.with_suffix("").name}')
            decompressed = dest.with_suffix('')
            # Limit expanded data as well as compressed archive entries. This protects
            # Actions runners from accidentally decompressing unbounded LZ4 streams.
            maximum = min(MAX_ENTRY_BYTES, budget[0])
            proc = subprocess.Popen([tool('lz4'), '-d', '-c', str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                assert proc.stdout is not None
                _copy_limited(proc.stdout, decompressed, maximum)
                proc.stdout.close()
                errors = proc.stderr.read() if proc.stderr else b''
                rc = proc.wait()
                if rc:
                    raise KitchenError('LZ4 decompression failed: ' + errors.decode(errors='replace')[-500:])
                budget[0] -= decompressed.stat().st_size
            except BaseException:
                proc.kill() if proc.poll() is None else None
                proc.wait()
                decompressed.unlink(missing_ok=True)
                raise
            dest = decompressed
        else:
            shutil.copyfile(path, dest)
        # Android sparse magic is 0xed26ff3a (little endian).
        with dest.open('rb') as inp:
            sparse = inp.read(4) == b'\x3a\xff\x26\xed'
        if sparse:
            raw = dest.with_name(dest.stem + '.raw.img')
            run(tool('simg2img'), dest, raw)
            dest.unlink()
            raw.rename(dest)


def unpack(source: Path, destination: Path) -> tuple[dict[str, Path], dict]:
    destination.mkdir(parents=True, exist_ok=True)
    _extract_archive(source, destination)
    metadata = {}
    if (destination / 'firmware.json').exists():
        try:
            metadata = json.loads((destination / 'firmware.json').read_text())
            if not isinstance(metadata, dict):
                raise KitchenError('firmware.json must contain a JSON object')
        except (ValueError, UnicodeError) as exc:
            raise KitchenError('Invalid firmware.json in firmware source') from exc
    super_img = destination / 'super.img'
    if super_img.exists():
        extracted = destination / 'logical'
        extracted.mkdir(exist_ok=True)
        # Preserve the stock logical layout for auditing. Some AOSP builds do
        # not support --json; the plain-text form is then retained instead.
        dumper = shutil.which('lpdump')
        if dumper:
            for flags, filename in ((['--json'], 'stock-super-metadata.json'), ([], 'stock-super-metadata.txt')):
                dump = subprocess.run([dumper, *flags, str(super_img)], capture_output=True, text=True)
                if dump.returncode == 0 and dump.stdout.strip():
                    (destination / filename).write_text(dump.stdout)
        run(tool('lpunpack'), super_img, extracted)
        for item in extracted.glob('*.img'):
            if (destination / item.name).exists():
                raise KitchenError(f'Duplicate partition from super.img: {item.name}')
            item.rename(destination / item.name)
        super_img.unlink()  # Prevent accidental reuse of an unmodified stock super image.
    result = {p.stem: p for p in destination.glob('*.img') if p.is_file()}
    if not result:
        raise KitchenError(f'No supported partition images discovered in {source.name}')
    return result, metadata


def base(name: str) -> str:
    return name[:-2] if name.endswith(('_a', '_b')) else name


def _slot(images: dict[str, Path]) -> str:
    slots = {key[-2:] for key in images if key.endswith(('_a', '_b'))}
    if not slots:
        return ''
    if slots != {'_a'}:
        raise KitchenError('Only the active _a slot is supported; provide slot-A partition images')
    return '_a'


def _metadata_check(donor: dict, target: dict, profile: dict) -> None:
    # Build compatibility cannot be proven by matching API; disallow known mismatches.
    for key in ('android_api', 'architecture'):
        dv, tv = donor.get(key), target.get(key)
        if dv is not None and tv is not None and dv != tv:
            raise KitchenError(f'Firmware mismatch for {key}: donor {dv}, target {tv}')
    dm, tm = donor.get('model'), target.get('model')
    if profile.get('donor_model') and dm and dm != profile['donor_model']:
        raise KitchenError('Donor model does not match device profile')
    if profile.get('target_model') and tm and tm != profile['target_model']:
        raise KitchenError('Target model does not match device profile')
    if not profile.get('target_model') or not profile.get('donor_model'):
        raise KitchenError('Profile must explicitly identify donor_model and target_model')
    if profile.get('compatibility_confirmed') is not True:
        raise KitchenError('Device profile needs compatibility_confirmed=true after device-specific review')


def selection(donor: dict[str, Path], target: dict[str, Path], profile: dict) -> dict[str, tuple[str, Path]]:
    dslot, tslot = _slot(donor), _slot(target)
    if dslot != tslot:
        raise KitchenError('Donor and target slot naming differ (A-only vs unsuffixed)')
    donor_parts = profile.get('donor_partitions', list(DEFAULT_DONOR))
    if not isinstance(donor_parts, list) or not donor_parts or not all(p in DEFAULT_DONOR for p in donor_parts):
        raise KitchenError('donor_partitions may only contain system, system_ext, product')
    if len(set(donor_parts)) != len(donor_parts):
        raise KitchenError('Duplicate donor partition names')
    missing = [part + tslot for part in donor_parts if part + tslot not in donor]
    if missing:
        raise KitchenError('Missing donor images: ' + ', '.join(missing))
    if not ('vendor' + tslot in target):
        raise KitchenError('Target vendor image required; never substitute donor vendor')
    if 'system' not in donor_parts:
        raise KitchenError('A system image must be selected from donor')
    result = {key: ('target', path) for key, path in target.items() if base(key) != 'super'}
    for part in donor_parts:
        key = part + tslot
        if key not in target:
            raise KitchenError(f'{key} missing from target; refusing to alter partition layout implicitly')
        result[key] = ('donor', donor[key])
    return result


def _align(n: int) -> int:
    return ((n + ALIGN - 1) // ALIGN) * ALIGN


def super_arguments(images: dict[str, Path], layout: dict, output: Path) -> list[str]:
    """Construct validated AOSP lpmake invocation from an explicit *target* layout."""
    size = layout.get('device_size')
    slots = layout.get('metadata_slots')
    meta = layout.get('metadata_size', 65536)
    groups = layout.get('groups')
    parts = layout.get('partitions')
    if not isinstance(size, int) or size < 1048576 or size % ALIGN:
        raise KitchenError('super.device_size must be a 4096-aligned byte count')
    if slots not in (1, 2, 3) or not isinstance(meta, int) or meta < 512 or meta % 512:
        raise KitchenError('Invalid metadata_slots or metadata_size')
    if not isinstance(groups, dict) or not groups or not isinstance(parts, dict) or not parts:
        raise KitchenError('Explicit super.groups and super.partitions are required')
    args = [tool('lpmake'), '--device-size', str(size), '--metadata-size', str(meta), '--metadata-slots', str(slots), '--sparse', '--output', str(output)]
    capacity = size - (2 * 4096 + meta * slots * 2) - ALIGN * (len(parts) + 2)
    group_used = {g: 0 for g in groups}
    for g, limit in groups.items():
        if not re.fullmatch(r'[A-Za-z][\w-]{0,63}', g) or not isinstance(limit, int) or limit <= 0 or limit > capacity:
            raise KitchenError(f'Invalid super group: {g}')
        args += ['--group', f'{g}:{limit}']
    for name, group in parts.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or group not in groups:
            raise KitchenError(f'Invalid super partition mapping: {name}')
        if name not in images:
            raise KitchenError(f'Missing required target-layout partition: {name}')
        path = images[name]
        aligned = _align(path.stat().st_size)
        group_used[group] += aligned
        args += ['--partition', f'{name}:readonly:{aligned}:{group}', '--image', f'{name}={path}']
    for group, used in group_used.items():
        if used > groups[group]:
            raise KitchenError(f'Super group {group} too small: used {used}, limit {groups[group]}')
    if sum(group_used.values()) > capacity:
        raise KitchenError('Partition images exceed available super device capacity')
    return args


def create_build(donor: Path, target: Path, profile: Path, output: Path, assemble_super: bool = False) -> dict:
    profile_data = json.loads(profile.read_text())
    if not isinstance(profile_data, dict):
        raise KitchenError('Device profile must contain a JSON object')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise KitchenError('Output directory must be empty to avoid stale artifacts')
    with tempfile.TemporaryDirectory(prefix='portkitchen-') as tmp:
        tmpdir = Path(tmp)
        donor_images, dmeta = unpack(donor, tmpdir / 'donor')
        target_images, tmeta = unpack(target, tmpdir / 'target')
        _metadata_check(dmeta, tmeta, profile_data)
        selected = selection(donor_images, target_images, profile_data)
        stage = output / 'images'
        stage.mkdir()
        manifest = {'project': 'PortKitchen', 'version': __version__, 'flashable': False,
                    'target_model': profile_data['target_model'], 'donor_model': profile_data['donor_model'],
                    'donor_sha256': hash_file(donor) if donor.is_file() else None,
                    'target_sha256': hash_file(target) if target.is_file() else None,
                    'parts': {}, 'warnings': [
                        'Not a verified bootable ROM. Requires device-specific patches, SELinux, AVB and recovery validation.',
                        'Never flash this build based on image assembly alone.' ]}
        for name, (source, path) in sorted(selected.items()):
            dest = stage / (name + '.img')
            shutil.copyfile(path, dest)
            manifest['parts'][name] = {'source': source, 'size': dest.stat().st_size,
                                       'sha256': hash_file(dest)}
        if assemble_super:
            layout = profile_data.get('super')
            if not layout:
                raise KitchenError('Assembly requires the target device super layout in profile.super')
            super_file = output / 'super.img'
            run(*super_arguments({p.stem: p for p in stage.glob('*.img')}, layout, super_file))
            # Verify that the generated image really contains valid logical metadata.
            run(tool('lpdump'), super_file)
            manifest['super'] = {'file': 'super.img', 'size': super_file.stat().st_size,
                                 'sha256': hash_file(super_file)}
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (output / 'READ_BEFORE_FLASHING.txt').write_text(
            'NOT VERIFIED BOOTABLE OR FLASHABLE.\n'
            'Check Android framework/vendor interface, kernel, camera, RIL, encryption,\n'
            'fstab, file_contexts, SELinux, AVB, partitions, dynamic groups and recovery.\n'
            'Do not flash automatically. Back up all partitions and user data.\n')
        return manifest


def create_profile_template(donor: Path, target: Path, output: Path) -> dict:
    """Inspect both firmware archives and create an explicitly unapproved recipe.

    This command deliberately does *not* claim that donor and target are compatible.
    Unlike a build, it does not stage multi-gigabyte images as artifacts.
    """
    if output.exists() and any(output.iterdir()):
        raise KitchenError('Output directory must be empty to avoid stale artifacts')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='portkitchen-review-') as temp:
        root = Path(temp)
        di, dm = unpack(donor, root / 'donor')
        ti, tm = unpack(target, root / 'target')
        ds = _slot(di)
        ts = _slot(ti)
        matched = [part for part in DEFAULT_DONOR if part + ds in di and part + ts in ti]
        profile = {
            'donor_model': dm.get('model', 'REVIEW_DONOR_MODEL'),
            'target_model': tm.get('model', 'REVIEW_TARGET_MODEL'),
            'compatibility_confirmed': False,
            'donor_partitions': matched or ['system'],
        }
        report = {
            'flashable': False,
            'donor_source_sha256': hash_file(donor) if donor.is_file() else None,
            'target_source_sha256': hash_file(target) if target.is_file() else None,
            'donor_metadata': dm,
            'target_metadata': tm,
            'donor_partitions': {key: image.stat().st_size for key, image in sorted(di.items())},
            'target_partitions': {key: image.stat().st_size for key, image in sorted(ti.items())},
            'donor_slot': ds or 'unsuffixed',
            'target_slot': ts or 'unsuffixed',
            'missing_in_donor': sorted(set(ti) - set(di)),
            'missing_in_target': sorted(set(di) - set(ti)),
            'needs_manual_review': [
                'Confirm architecture, Android API level and framework/vendor compatibility.',
                'Confirm the donor and target models: standard Samsung archives may not contain model metadata.',
                'Review stock dynamic partition geometry and available capacity.',
                'Plan device-specific RIL, camera, SELinux, fstab, VINTF, encryption and AVB adaptations.',
                'Never change compatibility_confirmed without a full compatibility review.',
            ],
        }
        for side in ('donor', 'target'):
            for ext in ('json','txt'):
                orig = root / side / ('stock-super-metadata.' + ext)
                if orig.is_file():
                    shutil.copyfile(orig, output / (side + '-super-metadata.' + ext))
    (output / 'profile-template.json').write_text(json.dumps(profile, indent=2) + '\n')
    (output / 'review.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'READ_ME_FIRST.txt').write_text(
        'This is a review TEMPLATE, not a compatible or flash-ready ROM.\n'
        'Complete device research before approving compatibility.\n'
        'Stock super metadata is included when the lpdump tool is available.\n'
    )
    return report
