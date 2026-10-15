#pragma once
#include <cwchar>
namespace qar {
inline bool ClassifyMediaArguments(const wchar_t* const* argv, int argc, bool* candidate) {
    if (candidate) *candidate = false;
    if (!argv || argc < 2 || !candidate) return false;
    unsigned types = 0, subtypes = 0;
    bool ppapi = false, audio = false, conflict = false;
    for (int i = 1; i < argc; ++i) {
        if (!argv[i]) return false;
        if (std::wcsncmp(argv[i], L"--type=", 7) == 0) {
            ++types;
            if (std::wcscmp(argv[i], L"--type=ppapi") == 0) { ppapi = true; *candidate = true; }
            else if (std::wcscmp(argv[i], L"--type=utility") != 0) conflict = true;
        }
        if (std::wcsncmp(argv[i], L"--utility-sub-type=", 19) == 0) {
            ++subtypes;
            if (std::wcscmp(argv[i], L"--utility-sub-type=audio.mojom.AudioService") == 0) { audio = true; *candidate = true; }
            else conflict = true;
        }
    }
    return !conflict && types == 1 && *candidate &&
        ((ppapi && subtypes == 0) || (!ppapi && audio && subtypes == 1));
}
}
