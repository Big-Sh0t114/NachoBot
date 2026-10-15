#include "audio_route_guard.h"
#include "hook_runtime.h"
#include "runtime_internal.h"
#include "media_command.h"

#include <aclapi.h>
#include <shellapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <cwchar>
#include <string>
#include <vector>

namespace qar {
namespace {

ConfigV1 g_childConfig = {};
StatusV1* g_childStatus = nullptr;
HANDLE g_configMapping = nullptr;
HANDLE g_statusMapping = nullptr;
HANDLE g_readyEvent = nullptr;
HANDLE g_failedEvent = nullptr;
std::wstring g_readyName;
std::wstring g_failedName;

std::wstring Hex64(uint64_t value) {
    static const wchar_t digits[] = L"0123456789abcdef";
    wchar_t buffer[17] = {};
    for (int i = 15; i >= 0; --i) {
        buffer[i] = digits[value & 0x0f];
        value >>= 4;
    }
    return std::wstring(buffer, 16);
}

std::wstring BaseName(uint32_t pid, uint64_t creation) {
    return L"Local\\QQCallAudioRouteGuard-v1." + std::to_wstring(pid) + L"." + Hex64(creation);
}

bool GetCurrentSid(PSID* sid, std::vector<BYTE>* storage) {
    if (sid) *sid = nullptr;
    if (!sid || !storage) return false;
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
    DWORD bytes = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &bytes);
    if (!bytes || bytes > 64 * 1024) {
        CloseHandle(token);
        return false;
    }
    storage->resize(bytes);
    const BOOL ok = GetTokenInformation(token, TokenUser, storage->data(), bytes, &bytes);
    CloseHandle(token);
    if (!ok) return false;
    *sid = reinterpret_cast<TOKEN_USER*>(storage->data())->User.Sid;
    return IsValidSid(*sid) != FALSE;
}

bool IsOwnerSid(HANDLE object, PSID expected) {
    if (!object || !expected) return false;
    PSID owner = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    const DWORD error = GetSecurityInfo(object, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION,
                                        &owner, nullptr, nullptr, nullptr, &descriptor);
    const bool matches = error == ERROR_SUCCESS && owner && EqualSid(owner, expected);
    if (descriptor) LocalFree(descriptor);
    return matches;
}

bool BoundedString(const wchar_t* value, size_t capacity, bool requireNonEmpty) {
    if (!value || capacity == 0) return false;
    const wchar_t* terminator = static_cast<const wchar_t*>(wmemchr(value, L'\0', capacity));
    if (!terminator) return false;
    return !requireNonEmpty || terminator != value;
}

bool EqualPath(const wchar_t* actual, const wchar_t* expected) {
    if (!BoundedString(actual, kMaxImagePathChars, true) ||
        !BoundedString(expected, kMaxImagePathChars, true)) return false;
    const int result = CompareStringOrdinal(actual, -1, expected, -1, TRUE);
    return result == CSTR_EQUAL;
}

bool GetImagePath(HANDLE process, std::wstring* path) {
    if (!process || !path) return false;
    std::vector<wchar_t> buffer(32768);
    DWORD size = static_cast<DWORD>(buffer.size());
    if (!QueryFullProcessImageNameW(process, 0, buffer.data(), &size) || size == 0) return false;
    path->assign(buffer.data(), size);
    return true;
}

bool GetProcessSid(HANDLE process, std::vector<BYTE>* storage, PSID* sid) {
    if (sid) *sid = nullptr;
    if (!storage || !sid) return false;
    HANDLE token = nullptr;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) return false;
    DWORD bytes = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &bytes);
    if (!bytes || bytes > 64 * 1024) {
        CloseHandle(token);
        return false;
    }
    storage->resize(bytes);
    const BOOL ok = GetTokenInformation(token, TokenUser, storage->data(), bytes, &bytes);
    CloseHandle(token);
    if (!ok) return false;
    *sid = reinterpret_cast<TOKEN_USER*>(storage->data())->User.Sid;
    return IsValidSid(*sid) != FALSE;
}

DWORD FindParentPid(DWORD childPid) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    DWORD parentPid = 0;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ProcessID == childPid) {
                parentPid = entry.th32ParentProcessID;
                break;
            }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return parentPid;
}

bool HasExactAudioServiceSwitches() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return false;
    bool candidate = false;
    const bool exact = ClassifyMediaArguments(argv, argc, &candidate);
    LocalFree(argv);
    return exact && candidate;
}

void SignalFailure(FailureCode code) {
    if (g_childStatus) {
        g_childStatus->failureCode = static_cast<uint32_t>(code);
        g_childStatus->phase = 2;
        MemoryBarrier();
    }
    if (!g_failedName.empty() && !g_failedEvent) g_failedEvent = OpenEventW(EVENT_MODIFY_STATE, FALSE, g_failedName.c_str());
    if (g_failedEvent) SetEvent(g_failedEvent);
}

FailureCode ValidateChildBinding(const ConfigV1* config) {
    if (!config || config->magic != kConfigMagic || config->size != sizeof(ConfigV1) ||
        config->size > kMaxConfigBytes || config->version != kConfigVersion ||
        config->flags != 0 || config->reserved != 0 || config->hostGeneration == 0 ||
        config->parentPid == 0 || config->childPid == 0 || config->parentPid == config->childPid ||
        config->actorSidBytes == 0 || config->actorSidBytes > kMaxSidBytes ||
        !IsValidSid(const_cast<BYTE*>(config->actorSid)) ||
        GetLengthSid(const_cast<BYTE*>(config->actorSid)) != config->actorSidBytes ||
        !BoundedString(config->qqBuild, kMaxBuildChars, true) ||
        !BoundedString(config->parentImage, kMaxImagePathChars, true) ||
        !BoundedString(config->childImage, kMaxImagePathChars, true) ||
        !BoundedString(config->captureEndpointId, kMaxEndpointIdChars, true) ||
        !BoundedString(config->renderEndpointId, kMaxEndpointIdChars, true) ||
        wcscmp(config->qqBuild, kSupportedQqBuild) != 0 ||
        wcscmp(config->captureEndpointId, config->renderEndpointId) == 0) {
        return config && BoundedString(config->qqBuild, kMaxBuildChars, true) &&
                       wcscmp(config->qqBuild, kSupportedQqBuild) != 0
                   ? FailureCode::build_unverified
                   : FailureCode::config_invalid;
    }

    IdentityObservationV1 observed = {};
    observed.childPid = GetCurrentProcessId();
    FILETIME creation = {}, exit = {}, kernel = {}, user = {};
    if (GetProcessTimes(GetCurrentProcess(), &creation, &exit, &kernel, &user))
        observed.childCreationTime = creation;
    observed.parentPid = FindParentPid(config->childPid);

    HANDLE parent = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, observed.parentPid);
    FILETIME parentCreation = {}, parentExit = {}, parentKernel = {}, parentUser = {};
    std::wstring parentImage;
    std::vector<BYTE> parentSidStorage;
    PSID parentSid = nullptr;
    if (parent) {
        if (GetProcessTimes(parent, &parentCreation, &parentExit, &parentKernel, &parentUser))
            observed.parentCreationTime = parentCreation;
        observed.parentImageMatches = GetImagePath(parent, &parentImage) &&
            EqualPath(parentImage.c_str(), config->parentImage);
        observed.parentActorMatches = GetProcessSid(parent, &parentSidStorage, &parentSid) &&
            EqualSid(parentSid, const_cast<BYTE*>(config->actorSid));
        CloseHandle(parent);
    }

    std::wstring childImage;
    observed.childImageMatches = GetImagePath(GetCurrentProcess(), &childImage) &&
        EqualPath(childImage.c_str(), config->childImage);
    observed.audioServiceCommandMatches = HasExactAudioServiceSwitches();

    std::vector<BYTE> actorStorage;
    PSID actorSid = nullptr;
    observed.childActorMatches = GetCurrentSid(&actorSid, &actorStorage) &&
        EqualSid(actorSid, const_cast<BYTE*>(config->actorSid));
    return ValidateObservedIdentity(*config, observed);
}

bool ReadConfigForThisChild() {
    FILETIME creation = {}, exit = {}, kernel = {}, user = {};
    if (!GetProcessTimes(GetCurrentProcess(), &creation, &exit, &kernel, &user)) return false;
    const std::wstring base = BaseName(GetCurrentProcessId(), FileTimeValue(creation));
    const std::wstring configName = base + L".config";
    const std::wstring statusName = base + L".status";
    g_failedName = base + L".failed";
    g_configMapping = OpenFileMappingW(FILE_MAP_READ | READ_CONTROL, FALSE, configName.c_str());
    if (!g_configMapping) return false;
    std::vector<BYTE> sidStorage;
    PSID sid = nullptr;
    if (!GetCurrentSid(&sid, &sidStorage) || !IsOwnerSid(g_configMapping, sid)) return false;
    const ConfigV1* config = static_cast<const ConfigV1*>(MapViewOfFile(g_configMapping, FILE_MAP_READ, 0, 0, sizeof(ConfigV1)));
    if (!config) return false;
    g_childConfig = *config;
    UnmapViewOfFile(config);
    g_statusMapping = OpenFileMappingW(FILE_MAP_WRITE | READ_CONTROL, FALSE, statusName.c_str());
    if (!g_statusMapping || !IsOwnerSid(g_statusMapping, sid)) return false;
    g_childStatus = static_cast<StatusV1*>(MapViewOfFile(g_statusMapping, FILE_MAP_WRITE, 0, 0, sizeof(StatusV1)));
    if (!g_childStatus || g_childStatus->magic != kConfigMagic ||
        g_childStatus->size != sizeof(StatusV1) || g_childStatus->version != kStatusVersion) return false;
    const std::wstring readyName = base + L".ready." + Hex64(g_childConfig.hostGeneration);
    g_readyEvent = OpenEventW(EVENT_MODIFY_STATE | READ_CONTROL, FALSE, readyName.c_str());
    g_failedEvent = OpenEventW(EVENT_MODIFY_STATE | READ_CONTROL, FALSE, g_failedName.c_str());
    return g_readyEvent && g_failedEvent && IsOwnerSid(g_readyEvent, sid) && IsOwnerSid(g_failedEvent, sid);
}

void CloseMappings() {
    if (g_childStatus) {
        UnmapViewOfFile(g_childStatus);
        g_childStatus = nullptr;
    }
    if (g_configMapping) {
        CloseHandle(g_configMapping);
        g_configMapping = nullptr;
    }
    if (g_statusMapping) {
        CloseHandle(g_statusMapping);
        g_statusMapping = nullptr;
    }
    if (g_readyEvent) {
        CloseHandle(g_readyEvent);
        g_readyEvent = nullptr;
    }
    if (g_failedEvent) {
        CloseHandle(g_failedEvent);
        g_failedEvent = nullptr;
    }
}

} // namespace

const ConfigV1* ActiveChildConfig() { return &g_childConfig; }
StatusV1* ActiveChildStatus() { return g_childStatus; }
void SetChildStatus(StatusV1* status) { g_childStatus = status; }
void CountInstalledHooks(uint64_t value) {
    if (g_childStatus) InterlockedExchange64(reinterpret_cast<volatile LONG64*>(
        &g_childStatus->installedHookCount), static_cast<LONG64>(value));
}
void CountReentry() {
    if (g_childStatus) InterlockedIncrement64(reinterpret_cast<volatile LONG64*>(
        &g_childStatus->reentryCount));
}
void CountRejectedActivation() {
    if (g_childStatus) InterlockedIncrement64(reinterpret_cast<volatile LONG64*>(
        &g_childStatus->rejectedActivationCount));
}

} // namespace qar

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        (void)instance;
    }
    return TRUE;
}

extern "C" __declspec(dllexport) DWORD WINAPI QQCallAudioRouteGuardChildStartV1(void*) {
    HMODULE pinned = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                       reinterpret_cast<LPCWSTR>(&QQCallAudioRouteGuardChildStartV1), &pinned);
    if (!qar::ReadConfigForThisChild()) {
        qar::SignalFailure(qar::FailureCode::config_invalid);
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::config_invalid);
    }
    const qar::FailureCode identity = qar::ValidateChildBinding(qar::ActiveChildConfig());
    if (identity != qar::FailureCode::none) {
        qar::SignalFailure(identity);
        qar::CloseMappings();
        return static_cast<DWORD>(identity);
    }
    if (!qar::VerifyCurrentProcessCfg()) {
        qar::SignalFailure(qar::FailureCode::mitigation_unverified);
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::mitigation_unverified);
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        qar::SignalFailure(qar::FailureCode::config_invalid);
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::config_invalid);
    }
    qar::ConfigV1 config = *qar::ActiveChildConfig();
    IMMDeviceEnumerator* raw = nullptr;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_INPROC_SERVER,
                          IID_PPV_ARGS(&raw));
    if (SUCCEEDED(hr) && raw) hr = qar::ValidateConfiguredEndpoints(raw, &config);
    if (raw) raw->Release();
    if (FAILED(hr)) {
        qar::SignalFailure(qar::FailureCode::target_invalid);
        CoUninitialize();
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::target_invalid);
    }
    hr = qar::InitializeGlobalInterfaceTable();
    if (FAILED(hr) || !qar::InstallChildApiHooks()) {
        qar::SignalFailure(qar::FailureCode::hook_install_failed);
        CoUninitialize();
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::hook_install_failed);
    }
    qar::StatusV1* status = qar::ActiveChildStatus();
    if (!status || status->hostGeneration != config.hostGeneration ||
        status->childPid != config.childPid ||
        !qar::FileTimeEqual(status->childCreationTime, config.childCreationTime)) {
        qar::SignalFailure(qar::FailureCode::child_identity_mismatch);
        CoUninitialize();
        qar::CloseMappings();
        return static_cast<DWORD>(qar::FailureCode::child_identity_mismatch);
    }
    status->phase = 1;
    MemoryBarrier();
    SetEvent(qar::g_readyEvent);
    // Keep the guard loaded and its MTA available for GIT-backed wrappers.
    // The process itself owns this thread until shutdown; the module is pinned.
    for (;;) Sleep(INFINITE);
}

#if defined(QAR_TESTING)
extern "C" __declspec(dllexport) DWORD WINAPI QarTestValidateChildBindingV1(const qar::ConfigV1* config) {
    return static_cast<DWORD>(qar::ValidateChildBinding(config));
}
#endif
