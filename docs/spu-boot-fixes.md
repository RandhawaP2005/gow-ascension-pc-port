# SPU boot fixes (BCAS25016)

These changes address defects found during review of the black-screen log.
They are not an end-to-end verified gameplay fix.

- Compile ps3recomp's `runtime/spu/spu_dispatch_mt.c` in the game target with
  Clang and `SPU_USE_DISPATCH_MT` for all lifted SPU sources. Fail configuration
  with an actionable message if the compiler or dispatcher is missing.
- Store r78 into SPU local store through `spu_ls_write128`, preserving guest
  big-endian word order. Do not replace this with a raw host-register memcpy.
- Lift WWS with absolute LS seeds `0xA2C,0xA68,0x3368,0x3378`. Validate the
  input ELF's machine, load address, entry and file-backed executable ranges
  before invoking the lifter. Normalize includes in both generated files.
- Label the IRQ diagnostic as an intermediate handler step. It previously
  claimed completion even when a trampoline could still be pending. Do not
  force PC to SRR0 or restore registers prematurely to make this message pass.

No game bytes or generated lifts are included, and runtime submodules are
unchanged. The validation checks layout, not the WWS module fingerprint;
continue using the extraction tool's fingerprint check on your own disc dump.

## Verification

From the repository root, using Python 3:

```powershell
python tools/test_spu_boot_fixes.py -v
python tools/relift_wws_pm.py --validate-only
```

The second command needs `spu_programs/wws_job_pm.elf`. If it rejects a relocated
ELF, re-extract the matching WWS policy module at LS 0xA00 rather than removing
validation or shifting the seeds. The test fixture uses synthetic ELF files
and does not prove that the real game or runtime builds.

## Windows rebuild

Use a developer shell containing Ninja and clang-cl, a compatible populated
ps3recomp tree/prebuilt runtime, the existing PPU/SPU registration tables and
your own game files. Do not reuse a build directory configured with a different
compiler. Check free disk space before regenerating/building.

```powershell
Get-PSDrive -PSProvider FileSystem
python tools/relift_wws_pm.py
cmake -S . -B build-spu-fix -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl
cmake --build build-spu-fix
.\build-spu-fix\gow_ascension.exe .\vfs\PS3_GAME\USRDIR\EBOOT.ELF
Get-PSDrive -PSProvider FileSystem
```

The relifter retains the existing symbol prefix/output directory; it does not
change the module fingerprint or registration scheme. If regenerating all SPU
images, regenerate their matching registration tables through the established
pipeline as well.

Confirm more than window FPS: the main BRB submitted/completed counts must
advance, workload 8 must finish jobs, new draw packets must continue beyond
the logo, and the log must stop reporting recursion-depth SPU halts. If it
still stalls, trace the first WWS interrupt through the actual guest iret to
SRR0, including register restoration, pending trampoline, list DMA resume and
queue cursor state. Do not force completion counters or disable synchronization.

## Local validation limits

Seven regression tests passed on macOS: ELF32/ELF64 acceptance, wrong load
address, wrong machine, entries only in BSS, truncated headers/segments,
validate-only behavior, and CMake dispatcher configuration/missing-source
failure. The runtime submodule, generated lifts and game data were absent in
the reviewed checkout, so no full compilation or Windows gameplay run was
possible. Graphify was unavailable, so its knowledge graph was not updated.
