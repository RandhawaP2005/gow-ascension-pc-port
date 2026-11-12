# WWS LS-0xA00 address candidate

This is a controlled address-layout experiment, not a verified black-screen fix.
It is opt-in (`GOW_WWS_ADDRESS_CANDIDATE=ON`). The default build remains the
baseline. No drain-return, synchronization, renderer, runtime-submodule source,
original PM ELF or baseline generated source changes are included.

## What changes in the candidate

- `tools/prepare_wws_a00.py` extracts the single executable payload from the
  existing zero-based (or A00-based) WWS ELF. It checks the 0x3000-byte payload
  against raw PM fingerprint `C8D774963A96DCF2` and wraps those exact bytes
  through this checkout's runtime wrapper at load/entry 0xA00.
- The runtime's lifter emits a separate WWS source/header with absolute seeds
  `0xA2C,0xA68,0x3368,0x3378`. No instruction operands are manually shifted.
- A copy of the workload registry uses the new entry `func_00000A00`, keeps
  image id 2 and all other images, preserves the raw fingerprint and updates
  the ELF-wrapper fingerprint to its new declared image extent.
- CMake replaces only the baseline WWS lift and workload registry with those
  candidate files. The candidate uses the supplied SPU_DRAIN/SPU_DIRECT runtime
  model; it does not add the old dispatch_mt source/macro from the earlier patch.
- For this build only, the legacy IRQ override (which called offset-0x40 code)
  and no-op dynamic-shim override are disabled. The generated IRQ entry and the
  runtime's dynamic-code interpreter/register-restoration path handle the IRQ.
- `GOW_WWS_LOAD_TRACE=1` logs the first 128 workload-8 command loads at rebased
  block 0x2500 (the load itself is at 0x2504). The preceding instruction does
  not change r3/r61. Trace records include r3/r61, the masked/aligned effective
  address and word0 there. This is a block-entry probe, not a per-instruction
  correctness oracle. The old zero-based PC probe is disabled for this build.

Preparation refuses to overwrite an existing candidate. It publishes its
output directory only after generation and checks succeed. Manifest hashes
cover input ELF, original registry, preparation/lifting tools, reviewed runtime
files and generated outputs. CMake rejects a changed/stale candidate. Runtime
source files in a shared submodule are never edited by this pipeline.

## Windows test procedure

Use the actual developer checkout with populated runtime, prebuilt compatible
runtime library, baseline generated sources/registry, Ninja/clang-cl and your
own game data. These dependencies are not supplied by this patch. Start in the
repository root. Use a fresh shell/environment matching the baseline renderer
and runtime settings; do not turn off interrupts or force flags/counters.

Record input state and free space:

```powershell
Set-Location "C:\IfItWorksItWorks\Project\punar's fork"
New-Item -ItemType Directory -Force scratch | Out-Null
git rev-parse HEAD
git -C ps3recomp rev-parse HEAD
git -C ps3recomp status --short
Get-PSDrive -PSProvider FileSystem
python tools/test_wws_a00_candidate.py -v
if ($LASTEXITCODE -ne 0) { throw 'Candidate regression tests failed' }
```

Generate the separate candidate. Stop on any error; do not relax guards:

```powershell
python tools/prepare_wws_a00.py 2>&1 | Tee-Object scratch/wws-a00-prepare.log
if ($LASTEXITCODE -ne 0) { throw 'WWS candidate preparation failed' }
```

The default input is `spu_programs/wws_job_pm.elf`; it stays unchanged. Outputs
are in `work/wws-ls-a00`. If that directory already exists, keep it for comparison
and choose a new `--out-dir`, passing the same path as `GOW_WWS_CANDIDATE_DIR`
to CMake. Do not overwrite/delete the baseline to satisfy a guard.

Configure and build (use the same renderer options as the baseline):

```powershell
if (Test-Path build-wws-a00) { throw 'Choose a new build directory' }
cmake -S . -B build-wws-a00 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DGOW_WWS_ADDRESS_CANDIDATE=ON 2>&1 | Tee-Object scratch/wws-a00-configure.log
if ($LASTEXITCODE -ne 0) { throw 'Candidate configuration failed' }
cmake --build build-wws-a00 2>&1 | Tee-Object scratch/wws-a00-build.log
if ($LASTEXITCODE -ne 0) { throw 'Candidate build failed' }
```

The configure output must say `WWS address candidate` and `entry LS 0xA00`.
The compile database must include the candidate WWS source and registry once
and exclude their two baseline counterparts. `upstream_compat.cpp` must have
`GOW_WWS_ADDRESS_CANDIDATE=1`.

Run the candidate executable, not the old build. Keep the same game arguments
and renderer flags as the baseline; the example uses the existing default:

```powershell
$env:GOW_WWS_LOAD_TRACE = '1'
.\build-wws-a00\gow_ascension.exe .\vfs\PS3_GAME\USRDIR\EBOOT.ELF 2>&1 | Tee-Object scratch/wws-a00-runtime.log
Remove-Item Env:GOW_WWS_LOAD_TRACE
Get-PSDrive -PSProvider FileSystem
```

Check the startup banner: `legacy A2C/3FEC0 overrides disabled`. A missing
banner indicates the wrong executable or configuration. Capture logs from
process start, not just the final hang. Return the four logs, manifest, new
WWS source/header/registry and compile commands for review.

Measure workload completion and BRB flag transitions, new rendered content,
continued draw/flip/present activity, SPU lane PCs/depths and any unresolved
branch, interpreter, validation or crash errors. A higher FPS or lack of an
audit-duration record is not a pass/fail criterion. Stop a hung run using the
normal window close; do not force synchronization to make it advance.

## Rollback and limits

Run the unchanged baseline executable for comparison. The candidate directory
and fresh build directory are independent; default OFF selects the baseline
again. Do not regenerate the baseline with the prior standalone relift command
for this test. If this experiment fails, retain the full logs rather than
changing the drain return check in the same run.

Local checks: eight new tests exercise synthetic ELF wrapping/lifting
orchestration, fingerprint and registration guards, source preservation,
stale-input/output rejection and actual CMake compile-command selection.
Seven existing tests also pass. The new pipeline tests use simulated wrapper
and lifter scripts, not the actual Windows runtime tools or game. No full
Windows build, real PM regeneration or gameplay validation was possible on
this Mac. Actual runtime register restoration and dynamic shim execution
remain part of the Windows experiment.
