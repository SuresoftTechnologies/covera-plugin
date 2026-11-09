"""CMake Visual Studio injection without changing compiler identification."""
from __future__ import annotations

import html
from pathlib import Path

from cover_build.core import errors


def is_visual_studio(generator: str | None) -> bool:
    return bool(generator and generator.startswith("Visual Studio "))


def property_sheet(wrappers: dict) -> str:
    compilers = {str(wrappers[role]) for role in ("c_compiler", "cxx_compiler") if role in wrappers}
    if len(compilers) > 1:
        raise errors.ExecutorError("CMAKE_MSBUILD_COMPILER_CONFLICT", "MSBuild C and C++ must select the same compiler wrapper")
    tools = {}
    if compilers:
        tools["CL"] = next(iter(compilers))
    for role, name in (("linker", "Link"), ("ar", "Lib")):
        if role in wrappers:
            tools[name] = str(wrappers[role])
    properties = []
    for name, value in tools.items():
        path = Path(value)
        properties.extend([
            f"<{name}ToolPath>{html.escape(path.parent.as_posix() + '/', quote=True)}</{name}ToolPath>",
            f"<{name}ToolExe>{html.escape(path.name, quote=True)}</{name}ToolExe>",
        ])
    body = "\n".join(properties)
    # Apply again before native tasks so project properties cannot silently
    # replace the import-time tool selection. Invocation logs remain decisive.
    return ('<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n'
            + '<PropertyGroup>\n' + body + '\n</PropertyGroup>\n'
            + '<Target Name="CoverCMakeSelectTools" BeforeTargets="ClCompile;Link;Lib">\n'
            + '<PropertyGroup>\n' + body + '\n</PropertyGroup>\n</Target>\n</Project>\n')


def cmake_patch(wrappers: dict) -> str:
    sheet = property_sheet(wrappers)
    if "]====]" in sheet:
        raise errors.ExecutorError("CMAKE_MSBUILD_PATH_INVALID", "Wrapper path conflicts with the CMake literal delimiter")
    # Append after add_subdirectory/add_library calls; retain every existing
    # VS_USER_PROPS import via a per-target forwarding property sheet.
    return r'''
# BEGIN COVER CMake Visual Studio wrapper injection
if(CMAKE_GENERATOR MATCHES "^Visual Studio ")
  file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/cover-ee-tools.props" [====[__SHEET__]====])
  function(_cover_ee_attach_props directory)
    get_property(_targets DIRECTORY "${directory}" PROPERTY BUILDSYSTEM_TARGETS)
    foreach(_target IN LISTS _targets)
      get_target_property(_type "${_target}" TYPE)
      if(_type MATCHES "^(EXECUTABLE|STATIC_LIBRARY|SHARED_LIBRARY|MODULE_LIBRARY|OBJECT_LIBRARY)$")
        get_target_property(_old "${_target}" VS_USER_PROPS)
        if(_old STREQUAL "_old-NOTFOUND")
          set(_old "$(UserRootDir)/Microsoft.Cpp.$(Platform).user.props")
        endif()
        if(_old AND NOT IS_ABSOLUTE "${_old}" AND NOT _old MATCHES "[$]")
          get_target_property(_binary "${_target}" BINARY_DIR)
          get_filename_component(_old "${_old}" ABSOLUTE BASE_DIR "${_binary}")
        endif()
        string(REPLACE "&" "&amp;" _old "${_old}")
        string(REPLACE "\"" "&quot;" _old "${_old}")
        string(REPLACE "<" "&lt;" _old "${_old}")
        string(REPLACE ">" "&gt;" _old "${_old}")
        string(MD5 _id "${_target}")
        set(_props "${_cover_ee_props_dir}/cover-ee-${_id}.props")
        file(WRITE "${_props}" "<Project xmlns=\"http://schemas.microsoft.com/developer/msbuild/2003\"><Import Project=\"${_old}\" Condition=\"Exists('${_old}')\"/><Import Project=\"cover-ee-tools.props\"/></Project>")
        set_property(TARGET "${_target}" PROPERTY VS_USER_PROPS "${_props}")
      endif()
    endforeach()
    get_property(_children DIRECTORY "${directory}" PROPERTY SUBDIRECTORIES)
    foreach(_child IN LISTS _children)
      _cover_ee_attach_props("${_child}")
    endforeach()
  endfunction()
  set(_cover_ee_props_dir "${CMAKE_CURRENT_BINARY_DIR}")
  _cover_ee_attach_props("${CMAKE_CURRENT_SOURCE_DIR}")
  unset(_cover_ee_props_dir)
endif()
# END COVER CMake Visual Studio wrapper injection
'''.replace("__SHEET__", sheet)
