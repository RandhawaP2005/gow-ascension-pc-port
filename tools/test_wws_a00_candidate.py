"""Synthetic pipeline and build-selection tests; no game/runtime binaries needed."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_wws_a00 as candidate
from test_spu_boot_fixes import elf_image

WRAPPER = '''import argparse, struct
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('raw'); p.add_argument('--base'); p.add_argument('--entry'); p.add_argument('--out')
a=p.parse_args(); base=int(a.base,0); entry=int(a.entry,0); blob=Path(a.raw).read_bytes()
ident=b'\\x7fELF'+bytes([1,2,1])+bytes(9)
h=struct.pack('>HH5I6H',2,23,1,entry,52,0,0,52,32,1,0,0,0)
ph=struct.pack('>8I',1,0x100,base,base,len(blob),len(blob),5,16)
Path(a.out).write_bytes((ident+h+ph).ljust(0x100,b'\\0')+blob)
'''
LIFTER = '''import argparse
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('elf'); p.add_argument('--auto-functions'); p.add_argument('--symbol-prefix'); p.add_argument('--extra-funcs'); p.add_argument('-o')
a=p.parse_args(); out=Path(a.o); out.mkdir(parents=True,exist_ok=True)
assert a.extra_funcs=='0xa2c,0xa68,0x3368,0x3378'
addresses=[0xA00,0x2500]+[int(x,0) for x in a.extra_funcs.split(',')]
names=[a.symbol_prefix+'spu_func_%08X'%x for x in addresses]
header='\\n'.join('void '+n+'(void);' for n in names)
source='static const spu_func_entry spu_function_table[] = {\\n'+'\\n'.join('{0x%08Xu, %s, "entry"},'%(a,n) for a,n in zip(addresses,names))+'\\n};'
(out/'spu_recomp.h').write_text(header)
(out/'spu_recomp.c').write_text(source)
'''


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'spu_programs/wws_job_pm.elf'
        self.source.parent.mkdir()
        self.source.write_bytes(elf_image(base=0))
        self.fingerprint = candidate.fnv(candidate.executable_segment(self.source))
        self.patch = mock.patch.object(candidate, 'EXPECTED_FNV', self.fingerprint)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.registry = self.root / 'src/gen/spu/spu_workloads.c'
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(
            'extern void '+candidate.OLD_ENTRY+'(spu_context*);\n'
            'void register_all(void) {\n'
            'spu_begin_image(2); spu_wws_job_pm_spu_recomp_register();\n'
            f'spu_workload_register_img(0x{self.fingerprint:X}ULL, {candidate.OLD_ENTRY}, 2, "wws_job_pm");\n'
            f'spu_workload_register_img(0x123ULL, {candidate.OLD_ENTRY}, 2, "wws_job_pm.elf");\n'
            'spu_workload_register_img(0x999ULL, other_entry, 5, "other");\n}\n')
        for name in candidate.RUNTIME_INPUTS:
            path = self.root / 'ps3recomp' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('/* synthetic runtime dependency */\n')
        (self.root/'ps3recomp/tools/wrap_spu_elf.py').write_text(WRAPPER)
        (self.root/'ps3recomp/tools/spu_lifter.py').write_text(LIFTER)
        (self.root/'ps3recomp/runtime/spu/spurs_policy.c').write_text('if (first) memcpy(ctx->ls + 0xA00, pm_host, pm_size);\n')
        (self.root/'tools').mkdir()
        for name in ('prepare_wws_a00.py','relift_wws_pm.py'):
            content=(ROOT/'tools'/name).read_text()
            if name=='prepare_wws_a00.py':
                content=content.replace('EXPECTED_FNV = 0xC8D774963A96DCF2', f'EXPECTED_FNV = 0x{self.fingerprint:X}')
            (self.root/'tools'/name).write_text(content)
        self.output = self.root/'work/wws-ls-a00'

    def prepare(self):
        candidate.prepare(self.root,self.source,self.output)

    def test_preparation_preserves_baseline_and_registers_correct_entries(self):
        before = (self.source.read_bytes(), self.registry.read_bytes())
        self.prepare()
        self.assertEqual(before,(self.source.read_bytes(),self.registry.read_bytes()))
        self.assertEqual(candidate.executable_segment(self.source), candidate.executable_segment(self.output/'wws_job_pm.elf'))
        registry=(self.output/'spu/spu_workloads.c').read_text()
        self.assertNotIn(candidate.OLD_ENTRY,registry)
        self.assertIn(candidate.ENTRY,registry)
        self.assertIn('0x999ULL, other_entry, 5, "other"',registry)
        self.assertIn(f'0x{candidate.elf_fingerprint(self.output/"wws_job_pm.elf"):016X}ULL',registry)
        candidate.verify(self.root,self.output)

    def test_wrong_fingerprint_is_rejected_before_generation(self):
        with mock.patch.object(candidate,'EXPECTED_FNV',0), self.assertRaisesRegex(ValueError,'Wrong PM fingerprint'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_stale_runtime_and_modified_candidate_are_rejected(self):
        self.prepare()
        path=self.root/'ps3recomp/runtime/spu/spu_drain.c'
        content=path.read_text(); path.write_text(content+'/* changed */')
        with self.assertRaisesRegex(ValueError,'stale'):
            candidate.verify(self.root,self.output)
        path.write_text(content)
        (self.output/'spu/wws_job_pm/spu_recomp.c').write_text('changed')
        with self.assertRaisesRegex(ValueError,'output changed'):
            candidate.verify(self.root,self.output)

    def test_existing_candidate_is_not_overwritten(self):
        self.output.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'Refusing to overwrite'):
            self.prepare()

    def test_wrong_registration_image_id_is_rejected(self):
        text=self.registry.read_text().replace(', 2, "wws_job_pm"',', 3, "wws_job_pm"')
        with self.assertRaisesRegex(ValueError,'image 2'):
            candidate.rewrite_registry(text,123)

    def test_missing_irq_entry_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'Missing generated entry'):
            candidate.verify_generated('static const spu_func_entry spu_function_table[] = {};','')

    def test_elf_fingerprint_excludes_bytes_outside_declared_extent(self):
        first=candidate.elf_fingerprint(self.source)
        self.source.write_bytes(self.source.read_bytes()+b'not part of image')
        self.assertEqual(first,candidate.elf_fingerprint(self.source))

    @unittest.skipUnless(shutil.which('cmake') and shutil.which('clang') and shutil.which('clang++'),'CMake and Clang required')
    def test_cmake_selects_only_candidate_wws_and_registry(self):
        self.prepare()
        shutil.copy2(ROOT/'CMakeLists.txt',self.root/'CMakeLists.txt')
        sources=['src/gen/spu/wws_job_pm/spu_recomp.c','src/gen/spu/other/spu_recomp.c',
                 'src/gen/ppu_recomp.c','src/compat/ascension_defaults.cpp','src/compat/upstream_compat.cpp',
                 'src/compat/psn_stubs.cpp','ps3recomp/runtime/ppu/tests/boot_main.cpp',
                 'ps3recomp/runtime/ppu/ppu_loader.cpp','ps3recomp/runtime/ppu/ppu_hle.cpp',
                 'ps3recomp/runtime/ppu/ppu_fs.cpp','ps3recomp/runtime/ppu/ppu_sysprx.cpp',
                 'ps3recomp/runtime/rsx/rsx_backend.c','ps3recomp/tools/gen_hle_nids.py','ps3recomp/tools/nid_database.py']
        for name in sources:
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.touch()
        args=['cmake','-S',str(self.root),'-B',str(self.root/'build'),
              '-DCMAKE_C_COMPILER='+shutil.which('clang'),'-DCMAKE_CXX_COMPILER='+shutil.which('clang++'),
              '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON','-DCMAKE_BUILD_TYPE=Release','-DGOW_WWS_ADDRESS_CANDIDATE=ON']
        result=subprocess.run(args,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        commands=json.loads((self.root/'build/compile_commands.json').read_text())
        files=[Path(x['file']).as_posix() for x in commands]
        self.assertNotIn(str(self.root/'src/gen/spu/wws_job_pm/spu_recomp.c'),files)
        self.assertNotIn(str(self.registry),files)
        for name in ('spu/wws_job_pm/spu_recomp.c','spu/spu_workloads.c'):
            self.assertEqual(files.count(str(self.output/name)),1)
        self.assertFalse(any('spu_dispatch_mt.c' in f for f in files))
        command=next(x['command'] for x in commands if x['file'].endswith('upstream_compat.cpp'))
        self.assertIn('GOW_WWS_ADDRESS_CANDIDATE=1',command)
        self.assertNotIn('SPU_USE_DISPATCH_MT',command)


if __name__=='__main__':
    unittest.main()
