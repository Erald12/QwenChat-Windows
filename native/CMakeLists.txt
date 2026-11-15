# Builds chatbridge.dll: chat_bridge.cpp + llama.cpp + ggml, all linked statically into ONE DLL.
# The app loads it with ctypes. The MSVC runtime is linked statically too (/MT), so the DLL needs
# nothing that Windows or Python doesn't already have.
#
# Configure with Visual Studio 2022 (or its Build Tools) - see build_windows.ps1.

cmake_minimum_required(VERSION 3.18)

# /MT instead of /MD: no msvcp140.dll needed on the user's PC. Must be set before project().
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>" CACHE STRING "" FORCE)

project(chatbridge C CXX)

set(LLAMA_CPP_DIR "${CMAKE_CURRENT_SOURCE_DIR}/llama.cpp" CACHE PATH "Path to the llama.cpp source tree")

# llama.cpp / ggml as static libraries, without tools, tests, examples, server or networking
set(BUILD_SHARED_LIBS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP      OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL        OFF CACHE BOOL "" FORCE)   # the app downloads models with Python, not llama.cpp
set(GGML_OPENMP          OFF CACHE BOOL "" FORCE)   # ggml's own thread pool: no vcomp140.dll needed

# CPU baseline. GGML_NATIVE would optimize for the build machine (a GitHub server), which may have
# instructions the user's PC lacks. AVX2 + FMA + F16C runs on Intel since 2013 (Haswell) and AMD
# since 2015 (Excavator / every Ryzen). For older CPUs: build_windows.ps1 -Avx2:$false
option(CHATBRIDGE_AVX2 "Build for AVX2 CPUs (else AVX only)" ON)
set(GGML_NATIVE  OFF CACHE BOOL "" FORCE)
set(GGML_AVX     ON  CACHE BOOL "" FORCE)
set(GGML_AVX2    ${CHATBRIDGE_AVX2} CACHE BOOL "" FORCE)
set(GGML_FMA     ${CHATBRIDGE_AVX2} CACHE BOOL "" FORCE)
set(GGML_F16C    ${CHATBRIDGE_AVX2} CACHE BOOL "" FORCE)
set(GGML_BMI2    ${CHATBRIDGE_AVX2} CACHE BOOL "" FORCE)
set(GGML_AVX512  OFF CACHE BOOL "" FORCE)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_subdirectory(${LLAMA_CPP_DIR} llama.cpp EXCLUDE_FROM_ALL)

add_library(chatbridge SHARED chat_bridge.cpp)
target_compile_features(chatbridge PRIVATE cxx_std_17)
target_link_libraries(chatbridge PRIVATE llama)

if (MSVC)
    target_compile_options(chatbridge PRIVATE /utf-8)
endif()

if (NOT WIN32)
    # Linux build (for testing the desktop app on Linux): export only the chat_* functions
    set_target_properties(chatbridge PROPERTIES
        C_VISIBILITY_PRESET hidden
        CXX_VISIBILITY_PRESET hidden
        VISIBILITY_INLINES_HIDDEN ON)
endif()
