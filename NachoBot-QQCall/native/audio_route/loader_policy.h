#pragma once
#include <cstddef>
#include <cwchar>

namespace qar {
// This whitelist is the observed cold-loader surface of QQ 9.9.26-44343.
// No QQNT/AVSDK/application or audio-client implementation is permitted here.
inline bool IsColdLoaderModule(const wchar_t* name) {
    if (!name) return false;
    const wchar_t* allowed[] = {L"qq.exe", L"ntdll.dll", L"kernel32.dll", L"kernelbase.dll",
        L"combase.dll", L"dbghelp.dll", L"msvcp_win.dll", L"oleaut32.dll",
        L"rpcrt4.dll", L"ucrtbase.dll", L"winmm.dll"};
    for (const auto* value : allowed) if (_wcsicmp(name, value) == 0) return true;
    return false;
}
inline bool IsOwnedLoaderLeaf(const unsigned char* code) {
    return code && (code[0] == 0x33 || code[0] == 0x31) && code[1] == 0xc0 && code[2] == 0xc3;
}
// Buffers referenced by a remote thread may be freed only after completion.
// On timeout the launch coordinator terminates the exact owned child; process
// teardown reclaims them without a race against a still-running thread.
inline bool CanReleaseRemoteThreadBuffer(unsigned long waitResult) { return waitResult == 0; }
}
