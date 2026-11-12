#!/usr/bin/env python3
"""Prepare an opt-in LS-0xA00 WWS build without changing baseline lifts."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from relift_wws_pm import EXTRA_FUNCS, validate_wws_elf

EXPECTED_FNV = 0xC8D774963A96DCF2
ENTRY = 'spu_wws_job_pm_spu_func_00000A00'
OLD_ENTRY = 'spu_wws_job_pm_spu_func_00000000'
RUNTIME_INPUTS = (
    'tools/spu_lifter.py', 'tools/wrap_spu_elf.py',
    'runtime/spu/spurs_policy.c', 'runtime/spu/spu_context.h',
    'runtime/spu/spu_drain.c', 'runtime/spu/spu_channels.c',
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fnv(data):
    value = 1469598103934665603  # matches this runtime's workload fingerprint
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def executable_segment(path):
    data = Path(path).read_bytes()
    if len(data) < 52 or data[:7] not in (b'\x7fELF\x01\x02\x01', b'\x7fELF\x02\x02\x01'):
        raise ValueError('Input must be a big-endian SPU ELF')
    if struct.unpack_from('>H', data, 18)[0] != 23:
        raise ValueError('Input is not EM_SPU')
    if data[4] == 1:
        entry, phoff = struct.unpack_from('>II', data, 24)
        stride, count = struct.unpack_from('>HH', data, 42)
        minimum, header = 32, 52
    else:
        if len(data) < 64:
            raise ValueError('Truncated ELF64 header')
        entry, phoff = struct.unpack_from('>QQ', data, 24)
        stride, count = struct.unpack_from('>HH', data, 54)
        minimum, header = 56, 64
    if not count or stride < minimum or phoff < header or phoff + stride * count > len(data):
        raise ValueError('Invalid program-header table')
    segments = []
    for index in range(count):
        off = phoff + index * stride
        if data[4] == 1:
            kind, offset, base, _, size, memsize, flags, _ = struct.unpack_from('>8I', data, off)
        else:
            kind, flags, offset, base, _, size, memsize, _ = struct.unpack_from('>II6Q', data, off)
        if kind == 1:
            if size > memsize or offset + size > len(data):
                raise ValueError('Truncated/invalid PT_LOAD')
            if flags & 1:
                segments.append((base, data[offset:offset + size]))
    if len(segments) != 1:
        raise ValueError('Expected exactly one executable PT_LOAD for the raw WWS PM')
    base, payload = segments[0]
    if entry != base or base not in (0, 0xA00) or len(payload) != 0x3000:
        raise ValueError('Expected a 0x3000-byte WWS image with matching base/entry 0 or 0xA00')
    return payload


def elf_fingerprint(path):
    """Match build_spu_workloads.img_size / runtime ELF image extent."""
    data = Path(path).read_bytes()
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x02\x01':
        raise ValueError('Runtime workload registration requires an ELF32 SPU wrapper')
    phoff, shoff = struct.unpack_from('>II', data, 28)
    phstride, phnum, shstride, shnum = struct.unpack_from('>4H', data, 42)
    if phstride < 32 or phoff + phstride * phnum > len(data):
        raise ValueError('Invalid ELF32 program-header table')
    if shnum and (shstride < 40 or shoff + shstride * shnum > len(data)):
        raise ValueError('Invalid ELF32 section-header table')
    extent = shoff + shstride * shnum
    for index in range(phnum):
        off = phoff + index * phstride
        offset, size = struct.unpack_from('>I', data, off + 4)[0], struct.unpack_from('>I', data, off + 16)[0]
        extent = max(extent, offset + size)
    for index in range(shnum):
        off = shoff + index * shstride
        if struct.unpack_from('>I', data, off + 4)[0] != 8:
            offset, size = struct.unpack_from('>II', data, off + 16)
            extent = max(extent, offset + size)
    return fnv(data[:min(extent, len(data))])


def rewrite_registry(text, elf_fp):
    # Preserve every other image id, constructor, include and registration.
    pattern = re.compile(
        r'spu_workload_register_img\(\s*(0x[0-9A-Fa-f]+)(?:ULL|ull|UL|ul)?\s*,\s*'
        + OLD_ENTRY + r'\s*,\s*(\d+)\s*,\s*"([^"\n]+)"\s*\)\s*;')
    matches = list(pattern.finditer(text))
    if not matches or any(int(m[2]) != 2 for m in matches):
        raise ValueError('Expected WWS workload registrations under image 2 and the old entry symbol')
    if sum(int(m[1], 16) == EXPECTED_FNV for m in matches) != 1:
        raise ValueError('Expected exactly one matching raw-PM fingerprint registration')

    def replace(match):
        fingerprint = EXPECTED_FNV if int(match[1], 16) == EXPECTED_FNV else elf_fp
        return f'spu_workload_register_img(0x{fingerprint:016X}ULL, {ENTRY}, 2, "{match[3]}");'

    result = pattern.sub(replace, text)
    result = result.replace(OLD_ENTRY, ENTRY)  # matching extern declaration
    if OLD_ENTRY in result:
        raise ValueError('Unconverted old entry reference')
    return result


def verify_generated(source, header):
    table = source.split('static const spu_func_entry spu_function_table[] =', 1)
    if len(table) != 2:
        raise ValueError('Unrecognized lifted function table; inspect the generated candidate')
    registered = {int(x, 16) for x in re.findall(r'\{\s*0x([0-9A-Fa-f]+)[uU]?\s*,', table[1])}
    for address in (0xA00, 0x2500) + EXTRA_FUNCS:
        symbol = f'spu_wws_job_pm_spu_func_{address:08X}'
        if address not in registered or symbol not in header:
            raise ValueError(f'Missing generated entry/table symbol at LS 0x{address:X}')
    if 0x3FEC0 in registered:
        raise ValueError('Dynamic shim 0x3FEC0 must not be registered as a static dispatch stub')


def verify(root, candidate):
    manifest = json.loads((candidate / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or manifest.get('load_base') != 0xA00:
        raise ValueError('Unrecognized WWS candidate manifest')
    for name, digest in manifest['inputs'].items():
        if sha(root / name) != digest:
            raise ValueError(f'Candidate is stale: {name} changed; prepare a new candidate')
    for name, digest in manifest['outputs'].items():
        if sha(candidate / name) != digest:
            raise ValueError(f'Candidate output changed: {name}; prepare a new candidate')
    validate_wws_elf(candidate / 'wws_job_pm.elf')
    if fnv(executable_segment(candidate / 'wws_job_pm.elf')) != EXPECTED_FNV:
        raise ValueError('Candidate PM fingerprint differs from the reviewed module')
    verify_generated((candidate / 'spu/wws_job_pm/spu_recomp.c').read_text(),
                     (candidate / 'spu/wws_job_pm/spu_recomp.h').read_text())
    print('WWS candidate verified: entry/load 0xA00, IRQ 0xA2C, image 2; inputs unchanged')


def prepare(root, source, destination):
    root = root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f'Refusing to overwrite {destination}; choose a fresh --out-dir')
    source = source.resolve()
    # Keep manifest inputs reproducible inside this checkout.
    source_name = source.relative_to(root).as_posix()
    registry_name = 'src/gen/spu/spu_workloads.c'
    registry = root / registry_name
    inputs = [source_name, registry_name, 'tools/relift_wws_pm.py', 'tools/prepare_wws_a00.py']
    inputs += ['ps3recomp/' + name for name in RUNTIME_INPUTS]
    hashes = {name: sha(root / name) for name in inputs}
    loader = (root / 'ps3recomp/runtime/spu/spurs_policy.c').read_text()
    if not re.search(r'memcpy\s*\(\s*ctx->ls\s*\+\s*0x[Aa]00\s*,\s*pm_host\s*,\s*pm_size\s*\)', loader):
        raise ValueError('Runtime loader is not the reviewed memcpy-to-LS-0xA00 form; review it first')
    payload = executable_segment(source)
    fingerprint = fnv(payload)
    if fingerprint != EXPECTED_FNV:
        raise ValueError(f'Wrong PM fingerprint 0x{fingerprint:016X}; expected 0x{EXPECTED_FNV:016X}')
    print(f'Input: {source}; raw PM unchanged, 0x{fingerprint:016X}', flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wws-a00-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        raw = stage / 'wws.bin'
        raw.write_bytes(payload)
        elf = stage / 'wws_job_pm.elf'
        subprocess.run([sys.executable, str(root / 'ps3recomp/tools/wrap_spu_elf.py'),
                        str(raw), '--base', '0xA00', '--entry', '0xA00', '--out', str(elf)], check=True)
        validate_wws_elf(elf)
        if executable_segment(elf) != payload:
            raise ValueError('Wrapper changed the raw PM bytes')
        output = stage / 'spu/wws_job_pm'
        output.mkdir(parents=True)
        subprocess.run([sys.executable, str(root / 'ps3recomp/tools/spu_lifter.py'), str(elf),
                        '--auto-functions', str(elf), '--symbol-prefix', 'spu_wws_job_pm_',
                        '--extra-funcs', ','.join(hex(a) for a in EXTRA_FUNCS), '-o', str(output)], check=True)
        for name in ('spu_recomp.c', 'spu_recomp.h'):
            path = output / name
            content = path.read_text()
            content = re.sub(r'#include\s+"(?:\.\./)+runtime/spu/([A-Za-z0-9_]+\.h)"',
                             r'#include "\1"', content)
            path.write_text(content)
        verify_generated((output / 'spu_recomp.c').read_text(), (output / 'spu_recomp.h').read_text())
        text = rewrite_registry(registry.read_text(), elf_fingerprint(elf))
        (stage / 'spu/spu_workloads.c').write_text(text)
        raw.unlink()  # only our temporary copy, never the original input
        outputs = {p.relative_to(stage).as_posix(): sha(p) for p in stage.rglob('*') if p.is_file()}
        manifest = dict(schema=1, load_base=0xA00, raw_fingerprint=f'{fingerprint:016X}',
                        inputs=hashes, outputs=outputs)
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        stage.rename(destination)
    verify(root, destination)
    print(f'Prepared {destination}; baseline ELF/lifts/registry and runtime sources unchanged')


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=root / 'spu_programs/wws_job_pm.elf')
    parser.add_argument('--out-dir', type=Path, default=root / 'work/wws-ls-a00')
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            verify(root, args.verify.resolve())
        else:
            prepare(root, args.elf, args.out_dir.resolve())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'WWS candidate preparation/verification failed: {exc}\n')


if __name__ == '__main__':
    main()
