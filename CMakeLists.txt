# God of War: Ascension (PS3) - Native x86-64 Static Recompilation
# Directly mirroring gow-ascension-pc/CMakeLists.txt (boot_main.cpp harness).

cmake_minimum_required(VERSION 3.20)
project(gow_ascension CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(NOT MSVC)
    add_compile_options(-fms-extensions)
endif()

set(PS3RECOMP_DIR "${CMAKE_CURRENT_SOURCE_DIR}/ps3recomp" CACHE PATH "ps3recomp repo root")

# Prefer prebuilt libps3recomp_runtime.a (MinGW UCRT) or ps3recomp_runtime.lib (MSVC)
if(EXISTS "${PS3RECOMP_DIR}/build/libps3recomp_runtime.a")
    set(PS3RECOMP_RUNTIME_LIB "${PS3RECOMP_DIR}/build/libps3recomp_runtime.a" CACHE FILEPATH "prebuilt runtime library")
else()
    set(PS3RECOMP_RUNTIME_LIB "${PS3RECOMP_DIR}/build/ps3recomp_runtime.lib" CACHE FILEPATH "prebuilt runtime library")
endif()

# The PPU boot scaffold is compiled here against this game's ppu_recomp.h
set(PS3RECOMP_SCAFFOLD
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_loader.cpp"
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_hle.cpp"
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_fs.cpp"
    "${PS3RECOMP_DIR}/runtime/ppu/ppu_sysprx.cpp"
)

# HLE NID table: generated for every PRX module in ps3recomp/libs (exactly as gow-ascension-pc does)
find_package(Python3 COMPONENTS Interpreter REQUIRED)
set(HLE_NID_TABLE "${CMAKE_CURRENT_BINARY_DIR}/ppu_hle_nids.cpp")
file(GLOB_RECURSE HLE_LIB_SOURCES CONFIGURE_DEPENDS "${PS3RECOMP_DIR}/libs/*.c")
add_custom_command(
    OUTPUT "${HLE_NID_TABLE}"
    COMMAND "${Python3_EXECUTABLE}"
            "${PS3RECOMP_DIR}/tools/gen_hle_nids.py"
            --out "${HLE_NID_TABLE}" --all
    DEPENDS "${PS3RECOMP_DIR}/tools/gen_hle_nids.py"
            "${PS3RECOMP_DIR}/tools/nid_database.py"
            ${HLE_LIB_SOURCES}
    COMMENT "Generating the HLE NID -> handler table (all modules)")

# Lifted game code (8 chunks + tables, excluding old src/gen/ppu_hle_nids.cpp)
file(GLOB GEN_SOURCES "src/gen/*.cpp" "src/gen/*.c")
list(REMOVE_ITEM GEN_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/src/gen/ppu_hle_nids.cpp")

# Lifted SPU images
file(GLOB_RECURSE SPU_SOURCES "src/gen/spu/*.c" "src/spu_gen/*.c")

# Controlled WWS address experiment: do not replace baseline generated files.
option(GOW_WWS_ADDRESS_CANDIDATE "Use the separately prepared LS-0xA00 WWS lift" OFF)
set(GOW_WWS_CANDIDATE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/work/wws-ls-a00" CACHE PATH
    "Candidate directory emitted by tools/prepare_wws_a00.py")
if(GOW_WWS_ADDRESS_CANDIDATE)
    execute_process(
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tools/prepare_wws_a00.py"
                --verify "${GOW_WWS_CANDIDATE_DIR}"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        RESULT_VARIABLE WWS_VERIFY_RESULT OUTPUT_VARIABLE WWS_VERIFY_OUT ERROR_VARIABLE WWS_VERIFY_ERR)
    if(NOT WWS_VERIFY_RESULT EQUAL 0)
        message(FATAL_ERROR "WWS candidate validation failed.\n${WWS_VERIFY_OUT}${WWS_VERIFY_ERR}")
    endif()
    list(FILTER SPU_SOURCES EXCLUDE REGEX "/wws_job_pm/")
    list(REMOVE_ITEM SPU_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/src/gen/spu/spu_workloads.c")
    list(APPEND SPU_SOURCES
        "${GOW_WWS_CANDIDATE_DIR}/spu/wws_job_pm/spu_recomp.c"
        "${GOW_WWS_CANDIDATE_DIR}/spu/spu_workloads.c")
    message(STATUS "WWS address candidate: ${GOW_WWS_CANDIDATE_DIR} (entry LS 0xA00, image 2)")
endif()

# Indirect SPU branches must be tail calls: ordinary calls grow the host
# stack on every guest loop iteration. Build the resolver with Clang here,
# as the reference port does, rather than in the prebuilt runtime library.
if(SPU_SOURCES AND NOT GOW_WWS_ADDRESS_CANDIDATE)
    if(NOT CMAKE_C_COMPILER_ID MATCHES "Clang")
        message(FATAL_ERROR
            "Lifted SPU code requires Clang's musttail dispatcher. "
            "Configure a fresh build directory with clang-cl on Windows "
            "(-DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl).")
    endif()
    set(SPU_DISPATCH_SOURCE "${PS3RECOMP_DIR}/runtime/spu/spu_dispatch_mt.c")
    if(NOT EXISTS "${SPU_DISPATCH_SOURCE}")
        message(FATAL_ERROR
            "Missing ${SPU_DISPATCH_SOURCE}; initialize a compatible ps3recomp runtime first.")
    endif()
    list(APPEND SPU_SOURCES "${SPU_DISPATCH_SOURCE}")
    set_property(SOURCE ${SPU_SOURCES} APPEND PROPERTY
        COMPILE_DEFINITIONS SPU_USE_DISPATCH_MT)
endif()

# LAW 4: Compile generated code in Release /O2 / -O2 without full debug PDBs to protect disk space on C:
if(MSVC)
    set_source_files_properties(${GEN_SOURCES} ${HLE_NID_TABLE} PROPERTIES COMPILE_FLAGS "/O2")
else()
    set_source_files_properties(${GEN_SOURCES} ${HLE_NID_TABLE} PROPERTIES COMPILE_FLAGS "-O2 -g0")
endif()

# RPCS3 RSX Dual-Renderer Architecture Option
# Phase 1: Ps3RsxBackend runtime abstraction layer with CLI switch (--renderer=d3d12 | --renderer=rpcs3-rsx).
# Phase 2 (Roadmap): Link RPCS3 RSX Vulkan backend subsystem (rsx_methods, VKGSRender, glslang, spirv-tools).
option(GOW_ENABLE_RPCS3_RSX "Enable RPCS3 RSX Vulkan backend" OFF)
set(ASCENSION_RPCS3_BUILD "${CMAKE_CURRENT_SOURCE_DIR}/work/gpu-build" CACHE PATH "Build tree of ps3recomp/gpu")
if(GOW_ENABLE_RPCS3_RSX)
    add_compile_definitions(GOW_ENABLE_RPCS3_RSX=1)
    message(STATUS "RPCS3 RSX Vulkan backend enabled")
else()
    add_compile_definitions(GOW_ENABLE_RPCS3_RSX=0)
endif()

option(ASCENSION_SPU_AVX2 "Lifted SPU code built for AVX2" OFF)
if(ASCENSION_SPU_AVX2)
    if(MSVC)
        set_source_files_properties(${SPU_SOURCES} PROPERTIES COMPILE_FLAGS "/O2 /arch:AVX2")
    else()
        set_source_files_properties(${SPU_SOURCES} PROPERTIES COMPILE_FLAGS "-O2 -g0 -mavx2 -mfma -mbmi2")
    endif()
else()
    if(MSVC)
        set_source_files_properties(${SPU_SOURCES} PROPERTIES COMPILE_FLAGS "/O2")
    else()
        set_source_files_properties(${SPU_SOURCES} PROPERTIES COMPILE_FLAGS "-O2 -g0")
    endif()
endif()

add_executable(gow_ascension
    # ps3recomp's full boot harness, as gow-ascension-pc builds it
    "${PS3RECOMP_DIR}/runtime/ppu/tests/boot_main.cpp"
    # Runtime defaults (21 values from gow-ascension-pc)
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compat/ascension_defaults.cpp"
    # Fallback symbols (cellFiberPpu, spurs diag)
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compat/upstream_compat.cpp"
    # PSN offline stubs
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compat/psn_stubs.cpp"
    "${PS3RECOMP_DIR}/runtime/rsx/rsx_backend.c"
    ${PS3RECOMP_SCAFFOLD}
    ${HLE_NID_TABLE}
    ${GEN_SOURCES}
    ${SPU_SOURCES}
)

file(GLOB LIB_INC_DIRS LIST_DIRECTORIES true "${PS3RECOMP_DIR}/libs/*")

target_include_directories(gow_ascension PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compat"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/gen"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/gen/spu"
    "${PS3RECOMP_DIR}/runtime/ppu"
    "${PS3RECOMP_DIR}/runtime/spu"
    "${PS3RECOMP_DIR}/runtime/syscalls"
    "${PS3RECOMP_DIR}"
    "${PS3RECOMP_DIR}/include"
    ${LIB_INC_DIRS}
)

target_link_libraries(gow_ascension PRIVATE "${PS3RECOMP_RUNTIME_LIB}")

if(GOW_WWS_ADDRESS_CANDIDATE)
    # The supplied runtime uses SPU_DRAIN/SPU_DIRECT, not the older
    # SPU_USE_DISPATCH_MT integration. Keep its dispatch implementation intact.
    target_compile_definitions(gow_ascension PRIVATE GOW_WWS_ADDRESS_CANDIDATE=1)
    target_include_directories(gow_ascension BEFORE PRIVATE "${GOW_WWS_CANDIDATE_DIR}/spu")
endif()

if(GOW_ENABLE_RPCS3_RSX)
    include("${PS3RECOMP_DIR}/gpu/ps3rsx.cmake")
    ps3rsx_link(gow_ascension "${ASCENSION_RPCS3_BUILD}")
endif()

if(WIN32)
    target_link_libraries(gow_ascension PRIVATE
        ws2_32 bcrypt xinput ole32 d3d12 dxgi dxguid d3dcompiler
        mfplat mfuuid winmm pdh)

    # 256 MB main thread stack reservation for deep PPC64 call nesting
    if(MSVC)
        target_link_options(gow_ascension PRIVATE /STACK:268435456 /MAP:gow_ascension.map)
    else()
        target_link_options(gow_ascension PRIVATE -static -Wl,--stack,268435456)
    endif()
endif()
