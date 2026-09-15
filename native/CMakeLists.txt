cmake_minimum_required(VERSION 3.20)
project(kadoka_native_runtime LANGUAGES CXX)

include(CTest)

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
    add_test(NAME kadoka_native_runtime_test COMMAND kadoka_native_runtime_test)
endif()
