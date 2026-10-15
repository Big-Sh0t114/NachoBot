#pragma once

#include "route_abi.h"

#include <string>

namespace qar {

struct ParentOptions {
    std::wstring qqImagePath;
    std::wstring guardDllPath;
    std::wstring qqBuild;
    std::wstring captureEndpointId;
    std::wstring renderEndpointId;
    uint64_t hostGeneration;
};

inline bool SameParentPolicy(const ParentOptions& left, const ParentOptions& right) {
    const auto samePath = [](const std::wstring& a, const std::wstring& b) {
        return !a.empty() && !b.empty() &&
            CompareStringOrdinal(a.c_str(), -1, b.c_str(), -1, TRUE) == CSTR_EQUAL;
    };
    return samePath(left.qqImagePath, right.qqImagePath) &&
        samePath(left.guardDllPath, right.guardDllPath) &&
        left.qqBuild == right.qqBuild && left.hostGeneration == right.hostGeneration &&
        left.captureEndpointId == right.captureEndpointId &&
        left.renderEndpointId == right.renderEndpointId;
}

bool InstallParentLaunchGuard(const ParentOptions& options, FailureCode* failure);
bool ParentLaunchGuardInstalled();
uint64_t ParentGuardGeneration();

} // namespace qar
