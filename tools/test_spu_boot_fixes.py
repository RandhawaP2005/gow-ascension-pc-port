"""Dependency-free regression checks; synthetic inputs contain no game code."""
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('relift_wws_pm', ROOT / 'tools/relift_wws_pm.py')
relift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relift)


def elf_image(bits=32, base=0xA00, machine=23, filesz=0x3000, memsz=0x3000):
    ident = b'\x7fELF' + bytes([1 if bits == 32 else 2, 2, 1]) + bytes(9)
    if bits == 32:
        header = struct.pack('>HH5I6H', 2, machine, 1, base, 52, 0, 0, 52, 32, 1, 0, 0, 0)
        ph = struct.pack('>8I', 1, 0x100, base, base, filesz, memsz, 5, 16)
    else:
        header = struct.pack('>HHI3QI6H', 2, machine, 1, base, 64, 0, 0, 64, 56, 1, 0, 0, 0)
        ph = struct.pack('>II6Q', 1, 5, 0x100, base, base, filesz, memsz, 16)
    return (ident + header + ph).ljust(0x100 + filesz, b'\0')


class WwsElfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'wws.elf'

    def validate(self, data):
        self.path.write_bytes(data)
        relift.validate_wws_elf(self.path)

    def test_elf32_and_elf64_use_absolute_addresses(self):
        for bits in (32, 64):
            with self.subTest(bits=bits):
                self.validate(elf_image(bits))

    def test_relocated_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'entry at LS 0xA00'):
            self.validate(elf_image(base=0))

    def test_ppu_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'EM_SPU'):
            self.validate(elf_image(machine=21))

    def test_seed_in_bss_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside executable file-backed'):
            self.validate(elf_image(filesz=0x100, memsz=0x3000))

    def test_truncated_headers_and_segments_are_rejected(self):
        for data in (elf_image()[:20], elf_image()[:70], elf_image()[:-1]):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                self.validate(data)

    def test_validate_only_does_not_launch_lifter(self):
        self.path.write_bytes(elf_image())
        with mock.patch('sys.argv', ['relift', '--elf', str(self.path), '--validate-only']), mock.patch.object(relift.subprocess, 'run') as run:
            relift.main()
            run.assert_not_called()


@unittest.skipUnless(shutil.which('cmake') and shutil.which('clang') and shutil.which('clang++'), 'CMake and Clang required')
class DispatchBuildTests(unittest.TestCase):
    def test_configure_routes_lifted_code_and_resolver_through_musttail(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shutil.copy2(ROOT / 'CMakeLists.txt', root / 'CMakeLists.txt')
            sources = [
                'src/gen/ppu_recomp.c', 'src/gen/spu/wws/spu_recomp.c',
                'src/compat/ascension_defaults.cpp', 'src/compat/upstream_compat.cpp', 'src/compat/psn_stubs.cpp',
                'ps3recomp/runtime/ppu/tests/boot_main.cpp', 'ps3recomp/runtime/ppu/ppu_loader.cpp',
                'ps3recomp/runtime/ppu/ppu_hle.cpp', 'ps3recomp/runtime/ppu/ppu_fs.cpp',
                'ps3recomp/runtime/ppu/ppu_sysprx.cpp', 'ps3recomp/runtime/rsx/rsx_backend.c',
                'ps3recomp/runtime/spu/spu_dispatch_mt.c', 'ps3recomp/tools/gen_hle_nids.py',
                'ps3recomp/tools/nid_database.py',
            ]
            for source in sources:
                path = root / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            result = subprocess.run(['cmake', '-S', str(root), '-B', str(root / 'build'),
                                     '-DCMAKE_C_COMPILER=' + shutil.which('clang'),
                                     '-DCMAKE_CXX_COMPILER=' + shutil.which('clang++'),
                                     '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commands = json.loads((root / 'build/compile_commands.json').read_text())
            for suffix in ('wws/spu_recomp.c', 'spu_dispatch_mt.c'):
                entries = [entry for entry in commands if entry['file'].endswith(suffix)]
                self.assertEqual(len(entries), 1, suffix)
                self.assertIn('SPU_USE_DISPATCH_MT', entries[0]['command'])
            # A runtime lacking the dispatcher must fail with actionable output.
            (root / 'ps3recomp/runtime/spu/spu_dispatch_mt.c').unlink()
            result = subprocess.run(['cmake', '-S', str(root), '-B', str(root / 'build')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('initialize a compatible ps3recomp runtime', result.stderr)


if __name__ == '__main__':
    unittest.main()
