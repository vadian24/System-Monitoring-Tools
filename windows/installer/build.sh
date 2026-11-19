#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

llvm_bin="${LLVM_BIN:-}"
if [[ -n "$llvm_bin" ]]; then
    llvm_bin="$(cd -- "$llvm_bin" && pwd)/"
fi

python3 build_payload.py
"${llvm_bin}llvm-dlltool" -m i386:x86-64 -d kernel32.def -l .build/kernel32.lib
"${llvm_bin}llvm-dlltool" -m i386:x86-64 -d user32.def -l .build/user32.lib
"${llvm_bin}clang" --target=x86_64-pc-windows-msvc -ffreestanding -fno-stack-protector \
    -Wall -Wextra -Werror -Os -c launcher.c -o .build/launcher.obj
"${llvm_bin}llvm-rc" /FO .build/setup.res setup.rc
"${llvm_bin}lld-link" /nologo /nodefaultlib /subsystem:console /entry:setup_entry /machine:x64 \
    /manifest:no /out:.build/SystemMonitorSetup.exe .build/launcher.obj .build/setup.res \
    .build/kernel32.lib .build/user32.lib
printf '%s\n' 'Built windows/installer/.build/SystemMonitorSetup.exe'
