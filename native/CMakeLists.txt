cmake_minimum_required(VERSION 3.20)
project(kadoka_native_runtime LANGUAGES CXX)

include(CTest)

option(KADOKA_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
if(KADOKA_ENABLE_SANITIZERS)
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
        add_link_options(-fsanitize=address,undefined)
    else()
        message(FATAL_ERROR "KADOKA_ENABLE_SANITIZERS requires a GCC- or Clang-compatible compiler")
    endif()
endif()

option(KADOKA_ENABLE_GCC_ANALYZER "Enable GCC static analysis" OFF)
if(KADOKA_ENABLE_GCC_ANALYZER)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        add_compile_options(-fanalyzer -Werror)
    else()
        message(FATAL_ERROR "KADOKA_ENABLE_GCC_ANALYZER requires GCC")
    endif()
endif()

add_library(kadoka_native_runtime SHARED
    src/runtime_api.cpp
)

target_include_directories(kadoka_native_runtime
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_compile_features(kadoka_native_runtime PUBLIC cxx_std_20)
target_compile_definitions(kadoka_native_runtime PRIVATE KADOKA_RUNTIME_BUILD)

if(MSVC)
    target_compile_options(kadoka_native_runtime PRIVATE /W4 /permissive-)
else()
    target_compile_options(kadoka_native_runtime PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(BUILD_TESTING)
    add_executable(kadoka_native_runtime_test tests/runtime_api_test.cpp)
    target_link_libraries(kadoka_native_runtime_test PRIVATE kadoka_native_runtime)
    target_compile_features(kadoka_native_runtime_test PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(kadoka_native_runtime_test PRIVATE /UNDEBUG)
    else()
        target_compile_options(kadoka_native_runtime_test PRIVATE -UNDEBUG)
    endif()
    add_test(NAME kadoka_native_runtime_test COMMAND kadoka_native_runtime_test)
endif()
