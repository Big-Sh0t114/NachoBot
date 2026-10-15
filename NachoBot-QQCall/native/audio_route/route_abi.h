#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <cstddef>
#include <cstdint>

// Private, fixed-size, versioned data shared only between the owned Host
// injector and the one suspended AudioService child it created.
namespace qar {

static const uint32_t kConfigMagic = 0x31524151; // "QAR1" little-endian.
static const uint32_t kConfigVersion = 1;
static const uint32_t kStatusVersion = 3;
enum class DiagnosticStage : uint32_t {
    create_enumerator, create_enumerator_ex, class_factory,
    register_notification, unregister_notification, enumerate_endpoints,
    default_endpoint, get_device, activate_device, unsupported_interface,
    property_value, wrap_enumerator, direct_sound_render, direct_sound_capture,
    direct_sound_duplex, wave_capture, wave_render, async_audio, count
};
static const uint32_t kMaxEndpointIdChars = 1024;
static const uint32_t kMaxImagePathChars = 1024;
static const uint32_t kMaxBuildChars = 32;
static const uint32_t kMaxSidBytes = SECURITY_MAX_SID_SIZE;
static const uint32_t kMaxConfigBytes = 16 * 1024;
static const wchar_t kSupportedQqBuild[] = L"9.9.26-44343";

enum class FailureCode : uint32_t {
    none = 0,
    config_invalid = 1001,
    actor_mismatch = 1002,
    child_identity_mismatch = 1003,
    parent_identity_mismatch = 1004,
    wrong_image_or_command = 1005,
    build_unverified = 1006,
    target_invalid = 1007,
    mitigation_unverified = 1008,
    hook_install_failed = 1009,
    existing_candidate_child = 1010,
    child_start_failed = 1011,
    child_not_ready = 1012,
};

struct ConfigV1 {
    uint32_t magic;
    uint32_t size;
    uint32_t version;
    uint32_t flags;
    uint32_t parentPid;
    uint32_t childPid;
    FILETIME parentCreationTime;
    FILETIME childCreationTime;
    uint64_t hostGeneration;
    uint32_t actorSidBytes;
    uint32_t reserved;
    BYTE actorSid[kMaxSidBytes];
    wchar_t qqBuild[kMaxBuildChars];
    wchar_t parentImage[kMaxImagePathChars];
    wchar_t childImage[kMaxImagePathChars];
    wchar_t captureEndpointId[kMaxEndpointIdChars];
    wchar_t renderEndpointId[kMaxEndpointIdChars];
};

struct StatusV1 {
    uint32_t magic;
    uint32_t size;
    uint32_t version;
    volatile uint32_t failureCode;
    volatile uint32_t phase;
    uint32_t childPid;
    FILETIME childCreationTime;
    uint64_t hostGeneration;
    uint64_t installedHookCount;
    uint64_t rejectedActivationCount;
    uint64_t reentryCount;
    uint64_t diagnosticCounts[static_cast<uint32_t>(DiagnosticStage::count)];
    int32_t diagnosticLastHresults[static_cast<uint32_t>(DiagnosticStage::count)];
    GUID lastUnsupportedIid;
    GUID lastActivationIid;
    uint32_t lastActivationFlow;
    uint32_t diagnosticReserved;
};

struct IdentityObservationV1 {
    uint32_t childPid;
    uint32_t parentPid;
    FILETIME childCreationTime;
    FILETIME parentCreationTime;
    bool childActorMatches;
    bool parentActorMatches;
    bool parentImageMatches;
    bool childImageMatches;
    bool audioServiceCommandMatches;
};

static_assert(sizeof(ConfigV1) <= kMaxConfigBytes, "audio route config exceeds fixed bound");
static_assert(sizeof(StatusV1) == 320, "audio route status layout changed");

inline uint64_t FileTimeValue(const FILETIME& value) {
    return (static_cast<uint64_t>(value.dwHighDateTime) << 32) | value.dwLowDateTime;
}

inline bool FileTimeEqual(const FILETIME& a, const FILETIME& b) {
    return a.dwHighDateTime == b.dwHighDateTime && a.dwLowDateTime == b.dwLowDateTime;
}

inline bool ValidateReadyStatus(const StatusV1& status, const ConfigV1& config) {
    return status.magic == kConfigMagic && status.size == sizeof(StatusV1) &&
        status.version == kStatusVersion && status.failureCode == 0 && status.phase == 1 &&
        status.childPid == config.childPid &&
        FileTimeEqual(status.childCreationTime, config.childCreationTime) &&
        status.hostGeneration == config.hostGeneration;
}

inline FailureCode ValidateObservedIdentity(const ConfigV1& config,
                                            const IdentityObservationV1& observed) {
    if (observed.childPid != config.childPid ||
        !FileTimeEqual(observed.childCreationTime, config.childCreationTime))
        return FailureCode::child_identity_mismatch;
    if (observed.parentPid != config.parentPid ||
        !FileTimeEqual(observed.parentCreationTime, config.parentCreationTime) ||
        !observed.parentImageMatches) return FailureCode::parent_identity_mismatch;
    if (!observed.childActorMatches || !observed.parentActorMatches)
        return FailureCode::actor_mismatch;
    if (!observed.childImageMatches || !observed.audioServiceCommandMatches)
        return FailureCode::wrong_image_or_command;
    return FailureCode::none;
}

} // namespace qar
