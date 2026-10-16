"""Command-line interface for PortKitchen."""
import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import KitchenError, create_build, hash_file, unpack


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='portkitchen', description='Samsung One UI donor/target firmware image kitchen')
    p.add_argument('--version', action='version', version=f'PortKitchen {__version__}')
    commands = p.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='Unpack firmware and list its partitions')
    inspect.add_argument('--firmware', required=True, type=Path)
    inspect.add_argument('--output', required=True, type=Path)
    build = commands.add_parser('build', help='Stage donor framework over target hardware partitions')
    for key in ('donor', 'target', 'profile', 'output'):
        build.add_argument('--' + key, required=True, type=Path)
    build.add_argument('--assemble-super', action='store_true', help='Build super.img with explicit target geometry')
    args = p.parse_args(argv)
    try:
        if args.command == 'inspect':
            images, meta = unpack(args.firmware, args.output)
            payload = {'metadata': meta, 'partitions': {key: {'size': image.stat().st_size, 'sha256': hash_file(image)} for key, image in sorted(images.items())}}
            (args.output / 'inspection.json').write_text(json.dumps(payload, indent=2) + '\n')
        else:
            payload = create_build(args.donor, args.target, args.profile, args.output, args.assemble_super)
        print(json.dumps(payload, indent=2))
        return 0
    except (KitchenError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'PortKitchen error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
