cmake_minimum_required(VERSION 3.20)
project(kadoka_native_runtime LANGUAGES CXX)

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
