#!/usr/bin/env python3
"""Lift the WWS PM at its SPURS local-store address, never at file offsets."""
import argparse
from pathlib import Path
import struct
import subprocess
import sys


EXTRA_FUNCS = (0xA2C, 0xA68, 0x3368, 0x3378)


def validate_wws_elf(path):
    """Reject relocated, truncated or non-SPU inputs before rewriting a lift.

    --auto-functions uses ELF virtual addresses. These reference seeds are
    absolute LS addresses for the PM loaded at 0xA00, not image offsets.
    """
    data = Path(path).read_bytes()
    if len(data) < 16 or data[:4] != b'\x7fELF' or data[5] != 2 or data[6] != 1:
        raise ValueError('Expected a big-endian SPU ELF (ELF version 1)')
    if data[4] == 1:
        header_size, ph_size = 52, 32
        if len(data) < header_size:
            raise ValueError('Truncated ELF header')
        entry, phoff = struct.unpack_from('>II', data, 24)
        phentsize, phnum = struct.unpack_from('>HH', data, 42)
    elif data[4] == 2:
        header_size, ph_size = 64, 56
        if len(data) < header_size:
            raise ValueError('Truncated ELF header')
        entry, phoff = struct.unpack_from('>QQ', data, 24)
        phentsize, phnum = struct.unpack_from('>HH', data, 54)
    else:
        raise ValueError('Unsupported ELF class')
    if struct.unpack_from('>H', data, 18)[0] != 23:  # EM_SPU
        raise ValueError('Expected EM_SPU, not a PPU or host executable')
    if entry != 0xA00:
        raise ValueError(f'Expected WWS entry at LS 0xA00, found 0x{entry:X}; re-extract the PM')
    if (not phnum or phentsize < ph_size or phoff < header_size or
            phoff + phnum * phentsize > len(data)):
        raise ValueError('Invalid or truncated program-header table')
    executable = []
    for i in range(phnum):
        off = phoff + i * phentsize
        if data[4] == 1:
            kind, offset, vaddr, _, filesz, memsz, flags, _ = struct.unpack_from('>8I', data, off)
        else:
            kind, flags, offset, vaddr, _, filesz, memsz, _ = struct.unpack_from('>II6Q', data, off)
        if kind != 1:
            continue
        if filesz > memsz or offset + filesz > len(data):
            raise ValueError('Invalid or truncated load segment')
        if flags & 1:
            executable.append((vaddr, vaddr + filesz))
    if not any(start == 0xA00 for start, _ in executable):
        raise ValueError('Expected an executable load segment at LS 0xA00')
    for addr in (0xA00,) + EXTRA_FUNCS:
        if not any(start <= addr and addr + 4 <= end for start, end in executable):
            raise ValueError(f'WWS entry 0x{addr:X} is outside executable file-backed bytes')


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=root / 'spu_programs/wws_job_pm.elf')
    parser.add_argument('--validate-only', action='store_true', help='Check ELF layout without generating files')
    args = parser.parse_args()
    try:
        validate_wws_elf(args.elf)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'WWS ELF validation failed: {exc}\n')
    if args.validate_only:
        print('WWS ELF layout validated: load/entry 0xA00; absolute LS entry seeds')
        return
    lifter = root / 'ps3recomp/tools/spu_lifter.py'
    if not lifter.is_file():
        parser.exit(1, 'Missing ps3recomp/tools/spu_lifter.py; initialize the runtime first\n')
    out_dir = root / 'src/gen/spu/wws_job_pm'
    cmd = [sys.executable, str(lifter), str(args.elf), '--auto-functions', str(args.elf),
           '--symbol-prefix', 'spu_wws_job_pm_', '--extra-funcs',
           ','.join(hex(addr) for addr in EXTRA_FUNCS), '-o', str(out_dir)]
    print(f'Running lifter: {subprocess.list2cmdline(cmd)}')
    subprocess.run(cmd, check=True)
    for name in ('spu_recomp.c', 'spu_recomp.h'):
        path = out_dir / name
        content = path.read_text(encoding='utf-8')
        content = content.replace('#include "../../runtime/spu/', '#include "')
        path.write_text(content, encoding='utf-8')
    print('Re-lift with absolute WWS entrypoints complete.')


if __name__ == '__main__':
    main()
