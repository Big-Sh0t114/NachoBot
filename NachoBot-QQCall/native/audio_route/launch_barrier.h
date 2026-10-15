#pragma once

#include <cstdint>

namespace qar {

enum class LaunchFailure : uint32_t {
    none = 0,
    create_failed = 1,
    invalid_created_identity = 2,
    bind_or_inject_failed = 3,
    ready_timeout = 4,
    replacement_detected = 5,
    resume_failed = 6,
};

struct ChildIdentity {
    uint32_t pid;
    uint64_t creationTime;
};

struct ProcessInformation {
    uintptr_t processHandle;
    uintptr_t threadHandle;
    ChildIdentity identity;
};

struct LaunchResult {
    bool success;
    bool callerRequestedSuspended;
    bool processInformationCleared;
    bool terminatedOwnedChild;
    bool resumedOwnedThread;
    uint32_t win32Error;
    LaunchFailure failure;
};

struct LaunchCallbacks {
    void* context;
    bool (*createSuspended)(void*, ProcessInformation*, uint32_t*);
    bool (*identityStillMatches)(void*, const ProcessInformation*);
    bool (*publishAndInject)(void*, const ProcessInformation*, uint32_t*);
    bool (*waitPolicyReady)(void*, const ProcessInformation*, uint32_t*);
    bool (*resumePrimaryThread)(void*, const ProcessInformation*, uint32_t*);
    void (*terminateOwnedChild)(void*, const ProcessInformation*);
    void (*closeReturnedHandles)(void*, const ProcessInformation*);
};

// The coordinator never returns a targeted child to its caller before the
// child has reported that identity checks, target validation, and required API
// hooks are ready. The Windows adapters provide exact process/thread handles.
inline LaunchResult RunLaunchBarrier(const LaunchCallbacks& callbacks,
                                    bool callerRequestedSuspended,
                                    ProcessInformation* output) {
    LaunchResult result = {};
    result.callerRequestedSuspended = callerRequestedSuspended;
    if (output == nullptr || callbacks.createSuspended == nullptr ||
        callbacks.identityStillMatches == nullptr || callbacks.publishAndInject == nullptr ||
        callbacks.waitPolicyReady == nullptr || callbacks.resumePrimaryThread == nullptr ||
        callbacks.terminateOwnedChild == nullptr || callbacks.closeReturnedHandles == nullptr) {
        result.win32Error = 87; // ERROR_INVALID_PARAMETER
        result.failure = LaunchFailure::create_failed;
        if (output != nullptr) *output = {};
        result.processInformationCleared = output != nullptr;
        return result;
    }

    *output = {};
    ProcessInformation created = {};
    uint32_t error = 0;
    if (!callbacks.createSuspended(callbacks.context, &created, &error)) {
        result.win32Error = error ? error : 31; // ERROR_GEN_FAILURE
        result.failure = LaunchFailure::create_failed;
        result.processInformationCleared = true;
        return result;
    }

    const auto failOwned = [&](LaunchFailure why, uint32_t code) {
        callbacks.terminateOwnedChild(callbacks.context, &created);
        callbacks.closeReturnedHandles(callbacks.context, &created);
        *output = {};
        result.terminatedOwnedChild = true;
        result.processInformationCleared = true;
        result.failure = why;
        result.win32Error = code ? code : 31;
    };

    if (created.processHandle == 0 || created.threadHandle == 0 || created.identity.pid == 0 ||
        created.identity.creationTime == 0) {
        failOwned(LaunchFailure::invalid_created_identity, 87);
        return result;
    }
    if (!callbacks.identityStillMatches(callbacks.context, &created)) {
        failOwned(LaunchFailure::replacement_detected, 1168); // ERROR_NOT_FOUND
        return result;
    }
    if (!callbacks.publishAndInject(callbacks.context, &created, &error)) {
        failOwned(LaunchFailure::bind_or_inject_failed, error);
        return result;
    }
    if (!callbacks.waitPolicyReady(callbacks.context, &created, &error)) {
        failOwned(LaunchFailure::ready_timeout, error ? error : 1460); // ERROR_TIMEOUT
        return result;
    }
    if (!callbacks.identityStillMatches(callbacks.context, &created)) {
        failOwned(LaunchFailure::replacement_detected, 1168);
        return result;
    }
    if (!callerRequestedSuspended &&
        !callbacks.resumePrimaryThread(callbacks.context, &created, &error)) {
        failOwned(LaunchFailure::resume_failed, error);
        return result;
    }

    *output = created;
    result.success = true;
    result.resumedOwnedThread = !callerRequestedSuspended;
    result.win32Error = 0;
    result.failure = LaunchFailure::none;
    return result;
}

} // namespace qar
