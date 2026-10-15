#pragma once

#include <windows.h>

#include <string>

namespace qar {

enum class LaunchClassification {
    unrelated,
    target_audio_service,
    reject_ambiguous_candidate,
};

inline LaunchClassification ClassifyLaunchPath(const std::wstring& resolvedPath,
                                                const std::wstring& configuredImage,
                                                bool candidateName,
                                                bool commandResolutionSucceeded,
                                                bool exactAudioServiceCommand) {
    if (!candidateName) return LaunchClassification::unrelated;
    if (!commandResolutionSucceeded || resolvedPath.empty())
        return LaunchClassification::reject_ambiguous_candidate;
    if (CompareStringOrdinal(resolvedPath.c_str(), -1, configuredImage.c_str(), -1, TRUE) != CSTR_EQUAL)
        return LaunchClassification::unrelated;
    return exactAudioServiceCommand ? LaunchClassification::target_audio_service :
        LaunchClassification::reject_ambiguous_candidate;
}

inline bool CommandPrefixNamesConfiguredImage(const std::wstring& command,
                                               const std::wstring& configuredImage) {
    const size_t imageSlash = configuredImage.find_last_of(L"\\/");
    const std::wstring imageName = imageSlash == std::wstring::npos ? configuredImage :
        configuredImage.substr(imageSlash + 1);
    if (imageName.empty()) return false;
    size_t prefixEnd = command.find(L"--utility-sub-type=");
    if (prefixEnd == std::wstring::npos) prefixEnd = command.size();
    for (size_t at = 0; at + imageName.size() <= prefixEnd; ++at) {
        const bool leftBoundary = at == 0 || command[at - 1] == L'\\' || command[at - 1] == L'/' ||
            command[at - 1] == L'"';
        const bool rightBoundary = at + imageName.size() == command.size() ||
            command[at + imageName.size()] == L' ' || command[at + imageName.size()] == L'\t' ||
            command[at + imageName.size()] == L'"';
        if (leftBoundary && rightBoundary && CompareStringOrdinal(command.data() + at,
            static_cast<int>(imageName.size()), imageName.c_str(), static_cast<int>(imageName.size()), TRUE) == CSTR_EQUAL)
            return true;
    }
    return false;
}

inline bool ExistingChildMustBlockInstall(bool processOpened, bool imagePathObserved,
                                           bool matchesConfiguredImage) {
    // An uninspectable direct child may already be the target; startup timing
    // cannot be proven while that possibility remains.
    return !processOpened || !imagePathObserved || matchesConfiguredImage;
}

} // namespace qar
