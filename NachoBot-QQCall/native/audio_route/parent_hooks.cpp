#include "parent_hooks.h"

#include "hook_runtime.h"
#include "launch_barrier.h"
#include "launch_classification.h"
#include "loader_policy.h"
#include "media_command.h"
#include "vendor/minhook/include/MinHook.h"

#include <aclapi.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <bcrypt.h>
#include <algorithm>
#include <atomic>
#include <cwchar>
#include <cstring>
#include <string>
#include <vector>

namespace qar {
namespace {

using CreateProcessWFn = BOOL (WINAPI*)(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES, LPSECURITY_ATTRIBUTES,
    BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
using CreateProcessAFn = BOOL (WINAPI*)(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES, LPSECURITY_ATTRIBUTES,
    BOOL, DWORD, LPVOID, LPCSTR, LPSTARTUPINFOA, LPPROCESS_INFORMATION);
using CreateProcessAsUserWFn = BOOL (WINAPI*)(HANDLE, LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
using CreateProcessAsUserAFn = BOOL (WINAPI*)(HANDLE, LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCSTR, LPSTARTUPINFOA, LPPROCESS_INFORMATION);
using CreateProcessWithTokenWFn = BOOL (WINAPI*)(HANDLE, DWORD, LPCWSTR, LPWSTR, DWORD, LPVOID,
    LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
using CreateProcessWithLogonWFn = BOOL (WINAPI*)(LPCWSTR, LPCWSTR, LPCWSTR, DWORD, LPCWSTR, LPWSTR,
    DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);

constexpr DWORD kReadyTimeoutMs = 30000;
constexpr DWORD kRemoteThreadTimeoutMs = 15000;
constexpr wchar_t kAudioSubtype[] = L"--utility-sub-type=audio.mojom.AudioService";
constexpr wchar_t kUtilityType[] = L"--type=utility";
constexpr wchar_t kPpapiType[] = L"--type=ppapi";

struct ParentState {
    ParentOptions options;
    std::wstring parentImage;
    DWORD parentPid;
    FILETIME parentCreation;
    std::vector<BYTE> sidStorage;
    PSID actorSid;
    HookBackend* backend;
    HookRequest requests[6];
    std::atomic<bool> installed;
    std::atomic<bool> hooksCommitted;
    std::atomic<uint64_t> childLaunches;
    FailureCode terminalFailure;
    ParentState() : parentPid(0), actorSid(nullptr), backend(nullptr),
        installed(false), hooksCommitted(false), childLaunches(0), terminalFailure(FailureCode::none) {}
};

ParentState g_state;
thread_local uint32_t g_parentHookDepth = 0;
std::atomic_flag g_installLock = ATOMIC_FLAG_INIT;

struct MappingState {
    HANDLE configMapping = nullptr;
    HANDLE statusMapping = nullptr;
    HANDLE readyEvent = nullptr;
    HANDLE failedEvent = nullptr;
    ConfigV1* config = nullptr;
    StatusV1* status = nullptr;
    std::wstring baseName;
    ~MappingState() {
        if (config) UnmapViewOfFile(config);
        if (status) UnmapViewOfFile(status);
        if (configMapping) CloseHandle(configMapping);
        if (statusMapping) CloseHandle(statusMapping);
        if (readyEvent) CloseHandle(readyEvent);
        if (failedEvent) CloseHandle(failedEvent);
    }
};

struct Attempt {
    BOOL (*invoke)(void*, DWORD, LPPROCESS_INFORMATION) = nullptr;
    void* invokeContext = nullptr;
    PROCESS_INFORMATION info = {};
    ChildIdentity identity = {};
    MappingState mapping;
};

struct InvokeContext {
    BOOL (*invoke)(void*, DWORD, LPPROCESS_INFORMATION) = nullptr;
    void* context = nullptr;
};

std::wstring Hex64(uint64_t value) {
    static const wchar_t digits[] = L"0123456789abcdef";
    wchar_t buffer[17] = {};
    for (int i = 15; i >= 0; --i) {
        buffer[i] = digits[value & 0x0f];
        value >>= 4;
    }
    return std::wstring(buffer, 16);
}

bool BoundedString(const std::wstring& value, size_t maximum) {
    return !value.empty() && value.size() < maximum && value.find(L'\0') == std::wstring::npos;
}

bool EqualPath(const std::wstring& left, const std::wstring& right) {
    return !left.empty() && !right.empty() &&
        CompareStringOrdinal(left.c_str(), -1, right.c_str(), -1, TRUE) == CSTR_EQUAL;
}

bool IsAbsolutePath(const std::wstring& value) {
    return value.size() >= 3 && ((value[0] >= L'A' && value[0] <= L'Z') ||
        (value[0] >= L'a' && value[0] <= L'z')) && value[1] == L':' &&
        (value[2] == L'\\' || value[2] == L'/');
}

bool GetImagePath(HANDLE process, std::wstring* path) {
    if (!process || !path) return false;
    std::vector<wchar_t> buffer(32768);
    DWORD size = static_cast<DWORD>(buffer.size());
    if (!QueryFullProcessImageNameW(process, 0, buffer.data(), &size) || size == 0) return false;
    path->assign(buffer.data(), size);
    return true;
}

bool GetTokenSid(HANDLE process, std::vector<BYTE>* storage, PSID* sid) {
    if (sid) *sid = nullptr;
    if (!process || !storage || !sid) return false;
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

bool ConvertAnsi(const char* value, std::wstring* wide, size_t maximum = kMaxImagePathChars) {
    if (!value || !wide) return false;
    const int length = MultiByteToWideChar(CP_ACP, 0, value, -1, nullptr, 0);
    if (length <= 1 || static_cast<size_t>(length) > maximum) return false;
    std::vector<wchar_t> buffer(static_cast<size_t>(length));
    if (!MultiByteToWideChar(CP_ACP, 0, value, -1, buffer.data(), length)) return false;
    wide->assign(buffer.data());
    return true;
}

bool ResolveApplicationPath(LPCWSTR app, LPWSTR command, LPCWSTR cwd, std::wstring* resolved,
                            bool* looksLikeQq) {
    if (resolved) resolved->clear();
    if (looksLikeQq) *looksLikeQq = false;
    if (!resolved || !looksLikeQq) return false;
    std::wstring token;
    if (app && *app) {
        token.assign(app);
    } else {
        if (!command || !*command) return false;
        int argc = 0;
        LPWSTR* argv = CommandLineToArgvW(command, &argc);
        if (!argv) return false;
        if (argc > 0) token.assign(argv[0]);
        LocalFree(argv);
    }
    if (token.empty()) return false;
    const size_t slash = token.find_last_of(L"\\/");
    const std::wstring tokenBase = slash == std::wstring::npos ? token : token.substr(slash + 1);
    const size_t targetSlash = g_state.options.qqImagePath.find_last_of(L"\\/");
    const std::wstring targetBase = targetSlash == std::wstring::npos ? g_state.options.qqImagePath :
        g_state.options.qqImagePath.substr(targetSlash + 1);
    std::wstring tokenStem = tokenBase;
    std::wstring targetStem = targetBase;
    const size_t tokenDot = tokenStem.find_last_of(L'.');
    const size_t targetDot = targetStem.find_last_of(L'.');
    if (tokenDot != std::wstring::npos) tokenStem.resize(tokenDot);
    if (targetDot != std::wstring::npos) targetStem.resize(targetDot);
    *looksLikeQq = CompareStringOrdinal(tokenBase.c_str(), -1, targetBase.c_str(), -1, TRUE) == CSTR_EQUAL ||
        CompareStringOrdinal(tokenStem.c_str(), -1, targetStem.c_str(), -1, TRUE) == CSTR_EQUAL;

    std::wstring search = cwd && *cwd ? cwd : L"";
    DWORD size = SearchPathW(search.empty() ? nullptr : search.c_str(), token.c_str(), L".exe",
                             0, nullptr, nullptr);
    if (!size || size > 32768) {
        if (*looksLikeQq) return false;
        return true;
    }
    std::vector<wchar_t> buffer(size + 1);
    const DWORD written = SearchPathW(search.empty() ? nullptr : search.c_str(), token.c_str(), L".exe",
                                      static_cast<DWORD>(buffer.size()), buffer.data(), nullptr);
    if (!written || written >= buffer.size()) return false;
    resolved->assign(buffer.data(), written);
    return true;
}

bool ClassifyAudioServiceCommand(LPWSTR command, bool* candidate) {
    if (candidate) *candidate = false;
    if (!command || !*command || !candidate) return false;
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(command, &argc);
    if (!argv) return false;
    const bool exact = ClassifyMediaArguments(argv, argc, candidate);
    LocalFree(argv);
    return exact;
}

bool HasRawAudioServiceSubtype(LPCWSTR command) {
    if (!command) return false;
    // PPAPI is a process type, not an AudioService utility subtype. AVSDK
    // opens its native clients in this child on the observed Windows build.
    const wchar_t* ppapi = wcsstr(command, kPpapiType);
    if (ppapi) {
        const wchar_t after = ppapi[ARRAYSIZE(kPpapiType) - 1];
        if (after == L'\0' || after == L' ' || after == L'\t' || after == L'"') return true;
    }
    static const wchar_t marker[] = L"--utility-sub-type=audio.mojom.AudioService";
    const size_t markerLength = ARRAYSIZE(marker) - 1;
    const size_t commandLength = wcslen(command);
    if (commandLength < markerLength) return false;
    for (size_t at = 0; at + markerLength <= commandLength; ++at) {
        if (CompareStringOrdinal(command + at, static_cast<int>(markerLength),
                marker, static_cast<int>(markerLength), TRUE) != CSTR_EQUAL) continue;
        const size_t after = at + markerLength;
        if (after == commandLength || command[after] == L' ' || command[after] == L'\t' ||
            command[after] == L'"') return true;
    }
    return false;
}

bool IsTargetLaunch(LPCWSTR app, LPWSTR command, LPCWSTR cwd, bool* ambiguousCandidate) {
    if (ambiguousCandidate) *ambiguousCandidate = false;
    if (!command) return false;
    bool commandCandidate = false;
    const bool exactCommand = ClassifyAudioServiceCommand(command, &commandCandidate);
    const bool rawSubtypeCandidate = HasRawAudioServiceSubtype(command);
    if (!commandCandidate && !rawSubtypeCandidate) return false;
    std::wstring resolved;
    bool candidateName = false;
    const bool resolvedCommand = ResolveApplicationPath(app, command, cwd, &resolved, &candidateName);
    if (!app && rawSubtypeCandidate && CommandPrefixNamesConfiguredImage(command, g_state.options.qqImagePath))
        candidateName = true;
    if (!resolvedCommand) {
        const LaunchClassification classification = ClassifyLaunchPath({}, g_state.options.qqImagePath,
            candidateName, false, exactCommand);
        if (ambiguousCandidate) *ambiguousCandidate = classification == LaunchClassification::reject_ambiguous_candidate;
        return classification == LaunchClassification::target_audio_service;
    }
    const LaunchClassification classification = ClassifyLaunchPath(resolved, g_state.options.qqImagePath,
        candidateName, true, exactCommand);
    if (ambiguousCandidate) *ambiguousCandidate = classification == LaunchClassification::reject_ambiguous_candidate;
    return classification == LaunchClassification::target_audio_service;
}

bool AnsiTargetImageName(std::string* imageName) {
    if (!imageName) return false;
    const size_t slash = g_state.options.qqImagePath.find_last_of(L"\\/");
    const std::wstring wideName = slash == std::wstring::npos ? g_state.options.qqImagePath :
        g_state.options.qqImagePath.substr(slash + 1);
    BOOL usedDefault = FALSE;
    const int bytes = WideCharToMultiByte(CP_ACP, 0, wideName.c_str(), -1, nullptr, 0,
                                          nullptr, &usedDefault);
    if (bytes <= 1 || usedDefault) return false;
    std::vector<char> converted(static_cast<size_t>(bytes));
    if (!WideCharToMultiByte(CP_ACP, 0, wideName.c_str(), -1, converted.data(), bytes,
                             nullptr, &usedDefault) || usedDefault) return false;
    imageName->assign(converted.data());
    return !imageName->empty();
}

bool AnsiRawAudioSubtype(LPCSTR command, size_t* markerPosition) {
    if (markerPosition) *markerPosition = 0;
    if (!command) return false;
    const char* ppapi = strstr(command, "--type=ppapi");
    if (ppapi) {
        const char after = ppapi[12];
        if (!after || after == ' ' || after == '\t' || after == '"') {
            if (markerPosition) *markerPosition = static_cast<size_t>(ppapi - command);
            return true;
        }
    }
    static const char marker[] = "--utility-sub-type=audio.mojom.AudioService";
    const size_t length = sizeof(marker) - 1;
    const size_t commandLength = strlen(command);
    for (size_t at = 0; at + length <= commandLength; ++at) {
        if (_strnicmp(command + at, marker, length) != 0) continue;
        const size_t after = at + length;
        if (after == commandLength || command[after] == ' ' || command[after] == '\t' || command[after] == '"') {
            if (markerPosition) *markerPosition = at;
            return true;
        }
    }
    return false;
}

bool AnsiCommandPrefixNamesTarget(LPCSTR command, const std::string& imageName, size_t prefixEnd) {
    if (!command || imageName.empty()) return false;
    const size_t commandLength = strlen(command);
    prefixEnd = std::min(prefixEnd, commandLength);
    if (prefixEnd < imageName.size()) return false;
    for (size_t at = 0; at + imageName.size() <= prefixEnd; ++at) {
        const bool leftBoundary = at == 0 || command[at - 1] == '\\' || command[at - 1] == '/' ||
            command[at - 1] == '"';
        const bool rightBoundary = at + imageName.size() == commandLength ||
            command[at + imageName.size()] == ' ' || command[at + imageName.size()] == '\t' ||
            command[at + imageName.size()] == '"';
        if (leftBoundary && rightBoundary && _strnicmp(command + at, imageName.c_str(), imageName.size()) == 0)
            return true;
    }
    return false;
}

bool LooksLikeUnclassifiableAnsiAudioCandidate(LPCSTR app, LPCSTR command) {
    size_t markerPosition = 0;
    if (!AnsiRawAudioSubtype(command, &markerPosition)) return false;
    std::string imageName;
    if (!AnsiTargetImageName(&imageName)) return true;
    if (app && *app) {
        const char* base = strrchr(app, '\\');
        const char* forward = strrchr(app, '/');
        if (!base || (forward && forward > base)) base = forward;
        base = base ? base + 1 : app;
        if (_stricmp(base, imageName.c_str()) == 0) return true;
    }
    return (!app || !*app) && AnsiCommandPrefixNamesTarget(command, imageName, markerPosition);
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

bool HasExistingQqChild() {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return true;
    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    bool found = false;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ParentProcessID != g_state.parentPid) continue;
            HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ProcessID);
            std::wstring image;
            const bool imageObserved = process && GetImagePath(process, &image);
            const bool match = imageObserved && EqualPath(image, g_state.options.qqImagePath);
            const bool mustBlock = ExistingChildMustBlockInstall(process != nullptr, imageObserved, match);
            if (process) CloseHandle(process);
            if (mustBlock) { found = true; break; }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return found;
}

bool MakeOwnerAttributes(SECURITY_ATTRIBUTES* attributes, SECURITY_DESCRIPTOR* descriptor, PACL* acl) {
    if (acl) *acl = nullptr;
    if (!attributes || !descriptor || !acl || !g_state.actorSid) return false;
    EXPLICIT_ACCESSW access = {};
    access.grfAccessPermissions = GENERIC_ALL;
    access.grfAccessMode = SET_ACCESS;
    access.grfInheritance = NO_INHERITANCE;
    access.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    access.Trustee.TrusteeType = TRUSTEE_IS_USER;
    access.Trustee.ptstrName = static_cast<LPWSTR>(g_state.actorSid);
    if (SetEntriesInAclW(1, &access, nullptr, acl) != ERROR_SUCCESS) return false;
    if (!InitializeSecurityDescriptor(descriptor, SECURITY_DESCRIPTOR_REVISION) ||
        !SetSecurityDescriptorOwner(descriptor, g_state.actorSid, FALSE) ||
        !SetSecurityDescriptorDacl(descriptor, TRUE, *acl, FALSE)) {
        LocalFree(*acl);
        *acl = nullptr;
        return false;
    }
    attributes->nLength = sizeof(*attributes);
    attributes->lpSecurityDescriptor = descriptor;
    attributes->bInheritHandle = FALSE;
    return true;
}

bool CreateChildMappings(Attempt* attempt) {
    if (!attempt || !attempt->identity.pid || !attempt->identity.creationTime) return false;
    const FILETIME childCreation = {static_cast<DWORD>(attempt->identity.creationTime),
                                    static_cast<DWORD>(attempt->identity.creationTime >> 32)};
    const std::wstring base = L"Local\\QQCallAudioRouteGuard-v1." + std::to_wstring(attempt->identity.pid) +
                              L"." + Hex64(attempt->identity.creationTime);
    attempt->mapping.baseName = base;
    SECURITY_DESCRIPTOR descriptor = {};
    PACL acl = nullptr;
    SECURITY_ATTRIBUTES attributes = {};
    if (!MakeOwnerAttributes(&attributes, &descriptor, &acl)) return false;
    const std::wstring configName = base + L".config";
    const std::wstring statusName = base + L".status";
    const std::wstring readyName = base + L".ready." + Hex64(g_state.options.hostGeneration);
    const std::wstring failedName = base + L".failed";
    attempt->mapping.configMapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &attributes, PAGE_READWRITE,
        0, sizeof(ConfigV1), configName.c_str());
    const DWORD configError = GetLastError();
    attempt->mapping.statusMapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &attributes, PAGE_READWRITE,
        0, sizeof(StatusV1), statusName.c_str());
    const DWORD statusError = GetLastError();
    attempt->mapping.readyEvent = CreateEventW(&attributes, TRUE, FALSE, readyName.c_str());
    const DWORD readyError = GetLastError();
    attempt->mapping.failedEvent = CreateEventW(&attributes, TRUE, FALSE, failedName.c_str());
    const DWORD failedError = GetLastError();
    LocalFree(acl);
    if (!attempt->mapping.configMapping || !attempt->mapping.statusMapping ||
        !attempt->mapping.readyEvent || !attempt->mapping.failedEvent ||
        configError == ERROR_ALREADY_EXISTS || statusError == ERROR_ALREADY_EXISTS ||
        readyError == ERROR_ALREADY_EXISTS || failedError == ERROR_ALREADY_EXISTS) return false;
    attempt->mapping.config = static_cast<ConfigV1*>(MapViewOfFile(attempt->mapping.configMapping,
        FILE_MAP_WRITE, 0, 0, sizeof(ConfigV1)));
    attempt->mapping.status = static_cast<StatusV1*>(MapViewOfFile(attempt->mapping.statusMapping,
        FILE_MAP_WRITE, 0, 0, sizeof(StatusV1)));
    if (!attempt->mapping.config || !attempt->mapping.status) return false;

    ConfigV1 config = {};
    config.magic = kConfigMagic;
    config.size = sizeof(ConfigV1);
    config.version = kConfigVersion;
    config.parentPid = g_state.parentPid;
    config.childPid = attempt->identity.pid;
    config.parentCreationTime = g_state.parentCreation;
    config.childCreationTime = childCreation;
    config.hostGeneration = g_state.options.hostGeneration;
    config.actorSidBytes = GetLengthSid(g_state.actorSid);
    if (config.actorSidBytes > kMaxSidBytes) return false;
    memcpy(config.actorSid, g_state.actorSid, config.actorSidBytes);
    wcscpy_s(config.qqBuild, g_state.options.qqBuild.c_str());
    wcscpy_s(config.parentImage, g_state.parentImage.c_str());
    wcscpy_s(config.childImage, g_state.options.qqImagePath.c_str());
    wcscpy_s(config.captureEndpointId, g_state.options.captureEndpointId.c_str());
    wcscpy_s(config.renderEndpointId, g_state.options.renderEndpointId.c_str());
    *attempt->mapping.config = config;

    StatusV1 status = {};
    status.magic = kConfigMagic;
    status.size = sizeof(StatusV1);
    status.version = kStatusVersion;
    status.childPid = attempt->identity.pid;
    status.childCreationTime = childCreation;
    status.hostGeneration = g_state.options.hostGeneration;
    *attempt->mapping.status = status;
    MemoryBarrier();
    return true;
}

bool ChildIdentityStillMatches(const PROCESS_INFORMATION& processInfo, const ChildIdentity& identity) {
    if (!processInfo.hProcess || GetProcessId(processInfo.hProcess) != identity.pid) return false;
    FILETIME creation = {}, exit = {}, kernel = {}, user = {};
    if (!GetProcessTimes(processInfo.hProcess, &creation, &exit, &kernel, &user) ||
        FileTimeValue(creation) != identity.creationTime) return false;
    if (FindParentPid(identity.pid) != g_state.parentPid) return false;
    std::wstring image;
    if (!GetImagePath(processInfo.hProcess, &image) || !EqualPath(image, g_state.options.qqImagePath)) return false;
    std::vector<BYTE> childSidStorage;
    PSID childSid = nullptr;
    return GetTokenSid(processInfo.hProcess, &childSidStorage, &childSid) &&
        EqualSid(childSid, g_state.actorSid);
}

bool FindRemoteModule(DWORD pid, const std::wstring& wantedPath, uintptr_t* base) {
    if (base) *base = 0;
    if (!base) return false;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snapshot == INVALID_HANDLE_VALUE) return false;
    MODULEENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    bool found = false;
    if (Module32FirstW(snapshot, &entry)) {
        do {
            if (EqualPath(entry.szExePath, wantedPath)) {
                *base = reinterpret_cast<uintptr_t>(entry.modBaseAddr);
                found = true;
                break;
            }
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return found;
}

bool VerifyColdExecutable() {
    // Pin the executable used by the successful owned loader experiment. A new
    // QQ build must be investigated before it can run this cold-loader path.
    static const BYTE expected[32] = {0x80,0x88,0x14,0x2f,0x4e,0x6b,0x31,0x61,
        0x5c,0x90,0xce,0x57,0x6b,0xea,0xf8,0x8d,0x3f,0xee,0x34,0x3c,0x15,0xd8,0xf6,0xe6,
        0x0b,0x4b,0xf8,0xcd,0x3d,0xf5,0x8e,0xea};
    if (g_state.options.qqBuild != L"9.9.26-44343") return false;
    HANDLE file = CreateFileW(g_state.options.qqImagePath.c_str(), GENERIC_READ, FILE_SHARE_READ,
        nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    BYTE digest[32] = {};
    BYTE buffer[16384];
    DWORD count = 0;
    bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0 &&
        BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0;
    while (ok) {
        if (!ReadFile(file, buffer, sizeof(buffer), &count, nullptr)) { ok = false; break; }
        if (!count) break;
        ok = BCryptHashData(hash, buffer, count, 0) >= 0;
    }
    if (ok) ok = BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
        memcmp(digest, expected, sizeof(digest)) == 0;
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    CloseHandle(file);
    if (!ok) SetLastError(ERROR_REVISION_MISMATCH);
    return ok;
}

__declspec(noinline) DWORD WINAPI OwnedLoaderNoOp(void*) { return 0; }

bool VerifyColdModules(DWORD pid) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snapshot == INVALID_HANDLE_VALUE) return false;
    wchar_t systemDirectory[MAX_PATH] = {};
    const UINT size = GetSystemDirectoryW(systemDirectory, ARRAYSIZE(systemDirectory));
    MODULEENTRY32W entry = {}; entry.dwSize = sizeof(entry);
    bool foundImage = false;
    bool foundKernelBase = false;
    bool ok = size && size < ARRAYSIZE(systemDirectory) && Module32FirstW(snapshot, &entry);
    size_t count = 0;
    while (ok) {
        if (++count > 32 || !IsColdLoaderModule(entry.szModule)) { ok = false; break; }
        if (_wcsicmp(entry.szModule, L"qq.exe") == 0) {
            foundImage = EqualPath(entry.szExePath, g_state.options.qqImagePath);
            if (!foundImage) { ok = false; break; }
        } else {
            const std::wstring wanted = std::wstring(systemDirectory) + L"\\" + entry.szModule;
            if (!EqualPath(entry.szExePath, wanted)) { ok = false; break; }
            if (_wcsicmp(entry.szModule, L"kernelbase.dll") == 0) foundKernelBase = true;
        }
        if (!Module32NextW(snapshot, &entry)) {
            ok = GetLastError() == ERROR_NO_MORE_FILES;
            break;
        }
    }
    CloseHandle(snapshot);
    if (!ok || !foundImage || !foundKernelBase) { SetLastError(ERROR_INVALID_STATE); return false; }
    return true;
}

bool WarmOwnedLoader(Attempt* attempt) {
    if (!attempt || !ChildIdentityStillMatches(attempt->info, attempt->identity) ||
        !VerifyColdExecutable()) return false;
    // Copy only our compiler-produced typed, relocation-free x64 leaf. Reject
    // toolchains producing a different body; never copy or guess vendor code.
    const BYTE* leaf = reinterpret_cast<const BYTE*>(&OwnedLoaderNoOp);
    if (!IsOwnedLoaderLeaf(leaf)) { SetLastError(ERROR_BAD_EXE_FORMAT); return false; }
    // Initial executable allocation gives the region a CFG bitmap. It is made
    // non-executable while writing; no page is writable and executable at once.
    constexpr SIZE_T regionBytes = 4096;
    void* remote = VirtualAllocEx(attempt->info.hProcess, nullptr, regionBytes,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READ);
    if (!remote) return false;
    DWORD oldProtect = 0;
    SIZE_T written = 0;
    CFG_CALL_TARGET_INFO target = {}; target.Offset = 0; target.Flags = CFG_CALL_TARGET_VALID;
    bool ok = VirtualProtectEx(attempt->info.hProcess, remote, regionBytes, PAGE_READWRITE, &oldProtect) &&
        WriteProcessMemory(attempt->info.hProcess, remote, leaf, 3, &written) && written == 3 &&
        VirtualProtectEx(attempt->info.hProcess, remote, regionBytes,
            PAGE_EXECUTE_READ | PAGE_TARGETS_NO_UPDATE, &oldProtect) &&
        SetProcessValidCallTargets(attempt->info.hProcess, remote, regionBytes, 1, &target) &&
        FlushInstructionCache(attempt->info.hProcess, remote, 3);
    HANDLE thread = ok ? CreateRemoteThread(attempt->info.hProcess, nullptr, 0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(remote), nullptr, 0, nullptr) : nullptr;
    DWORD wait = thread ? WaitForSingleObject(thread, kRemoteThreadTimeoutMs) : WAIT_OBJECT_0;
    DWORD code = 1;
    ok = thread && wait == WAIT_OBJECT_0 && GetExitCodeThread(thread, &code) && code == 0;
    const DWORD error = wait == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
    if (thread) CloseHandle(thread);
    if (CanReleaseRemoteThreadBuffer(wait)) VirtualFreeEx(attempt->info.hProcess, remote, 0, MEM_RELEASE);
    if (!ok) { SetLastError(error ? error : ERROR_DLL_INIT_FAILED); return false; }
    // Only add and then remove our own suspend increment; never run primary.
    const DWORD previous = SuspendThread(attempt->info.hThread);
    if (previous == static_cast<DWORD>(-1)) return false;
    const DWORD restored = ResumeThread(attempt->info.hThread);
    if (previous != 1 || restored != previous + 1) { SetLastError(ERROR_INVALID_STATE); return false; }
    return ChildIdentityStillMatches(attempt->info, attempt->identity) &&
        VerifyColdModules(attempt->info.dwProcessId);
}

bool RemoteLoadGuardDll(Attempt* attempt) {
    if (!attempt || !attempt->info.hProcess || !attempt->info.dwProcessId) return false;
    PROCESS_MITIGATION_CONTROL_FLOW_GUARD_POLICY cfg = {};
    if (!GetProcessMitigationPolicy(attempt->info.hProcess, ProcessControlFlowGuardPolicy, &cfg, sizeof(cfg)) ||
        !cfg.EnableControlFlowGuard) return false;
    PROCESS_MITIGATION_DYNAMIC_CODE_POLICY dynamicCode = {};
    if (!GetProcessMitigationPolicy(attempt->info.hProcess, ProcessDynamicCodePolicy,
                                    &dynamicCode, sizeof(dynamicCode)) || dynamicCode.ProhibitDynamicCode) return false;
    PROCESS_MITIGATION_BINARY_SIGNATURE_POLICY signature = {};
    if (!GetProcessMitigationPolicy(attempt->info.hProcess, ProcessSignaturePolicy,
                                    &signature, sizeof(signature)) ||
        signature.MicrosoftSignedOnly || signature.StoreSignedOnly) return false;

    if (!WarmOwnedLoader(attempt)) return false;

    HMODULE kernelBase = GetModuleHandleW(L"KernelBase.dll");
    FARPROC loadLibrary = kernelBase ? GetProcAddress(kernelBase, "LoadLibraryW") : nullptr;
    if (!kernelBase || !loadLibrary || !IsCfgImageTarget(loadLibrary, true)) return false;
    std::wstring kernelBasePath(32768, L'\0');
    DWORD kernelPathLength = GetModuleFileNameW(kernelBase, kernelBasePath.data(),
                                                static_cast<DWORD>(kernelBasePath.size()));
    if (!kernelPathLength || kernelPathLength >= kernelBasePath.size()) return false;
    kernelBasePath.resize(kernelPathLength);
    uintptr_t remoteKernelBase = 0;
    if (!FindRemoteModule(attempt->info.dwProcessId, kernelBasePath, &remoteKernelBase)) return false;
    const uintptr_t remoteLoadLibrary = remoteKernelBase +
        (reinterpret_cast<uintptr_t>(loadLibrary) - reinterpret_cast<uintptr_t>(kernelBase));

    HMODULE self = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(&RemoteLoadGuardDll), &self)) return false;
    wchar_t dllPath[32768] = {};
    const DWORD dllPathLength = GetModuleFileNameW(self, dllPath, ARRAYSIZE(dllPath));
    if (!dllPathLength || dllPathLength >= ARRAYSIZE(dllPath)) return false;
    // The .node and the child DLL are siblings by the build contract.
    const std::wstring remoteDllPath = g_state.options.guardDllPath;
    if (!IsAbsolutePath(remoteDllPath)) return false;
    DWORD attrs = GetFileAttributesW(remoteDllPath.c_str());
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) return false;

    const SIZE_T pathBytes = (remoteDllPath.size() + 1) * sizeof(wchar_t);
    void* remotePath = VirtualAllocEx(attempt->info.hProcess, nullptr, pathBytes, MEM_COMMIT | MEM_RESERVE,
                                      PAGE_READWRITE);
    if (!remotePath) return false;
    SIZE_T written = 0;
    bool ok = WriteProcessMemory(attempt->info.hProcess, remotePath, remoteDllPath.c_str(), pathBytes, &written) &&
              written == pathBytes;
    HANDLE loadThread = nullptr;
    if (ok) loadThread = CreateRemoteThread(attempt->info.hProcess, nullptr, 0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(remoteLoadLibrary), remotePath, 0, nullptr);
    if (!loadThread) ok = false;
    DWORD loadWait = WAIT_OBJECT_0;
    if (ok) {
        loadWait = WaitForSingleObject(loadThread, kRemoteThreadTimeoutMs);
        DWORD code = 0;
        ok = loadWait == WAIT_OBJECT_0 && GetExitCodeThread(loadThread, &code) && code != 0;
        if (loadWait == WAIT_TIMEOUT) SetLastError(ERROR_TIMEOUT);
    }
    const DWORD loadError = GetLastError();
    if (loadThread) CloseHandle(loadThread);
    if (CanReleaseRemoteThreadBuffer(loadWait)) VirtualFreeEx(attempt->info.hProcess, remotePath, 0, MEM_RELEASE);
    if (!ok) { SetLastError(loadError ? loadError : ERROR_DLL_INIT_FAILED); return false; }

    uintptr_t remoteDllBase = 0;
    if (!FindRemoteModule(attempt->info.dwProcessId, remoteDllPath, &remoteDllBase)) return false;
    HMODULE localDll = LoadLibraryExW(remoteDllPath.c_str(), nullptr, DONT_RESOLVE_DLL_REFERENCES);
    if (!localDll) return false;
    FARPROC childStart = GetProcAddress(localDll, "QQCallAudioRouteGuardChildStartV1");
    if (!childStart || !IsCfgImageTarget(childStart, true)) { FreeLibrary(localDll); return false; }
    const uintptr_t startRva = reinterpret_cast<uintptr_t>(childStart) - reinterpret_cast<uintptr_t>(localDll);
    FreeLibrary(localDll);
    const uintptr_t remoteStart = remoteDllBase + startRva;
    HANDLE startThread = CreateRemoteThread(attempt->info.hProcess, nullptr, 0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(remoteStart), nullptr, 0, nullptr);
    if (!startThread) return false;
    // The typed child bootstrap owns this thread for the lifetime of the child.
    CloseHandle(startThread);
    return true;
}

bool CreateAndInject(void* context, ProcessInformation* result, uint32_t* error) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt || !result || !error) return false;
    *result = {};
    attempt->info = {};
    if (!attempt->invoke(attempt->invokeContext, CREATE_SUSPENDED, &attempt->info)) {
        *error = GetLastError();
        return false;
    }
    if (!attempt->info.hProcess || !attempt->info.hThread || !attempt->info.dwProcessId) {
        if (attempt->info.hProcess) {
            TerminateProcess(attempt->info.hProcess, ERROR_INVALID_DATA);
            WaitForSingleObject(attempt->info.hProcess, 5000);
        }
        if (attempt->info.hThread) CloseHandle(attempt->info.hThread);
        if (attempt->info.hProcess) CloseHandle(attempt->info.hProcess);
        attempt->info = {};
        *error = ERROR_INVALID_DATA;
        return false;
    }
    FILETIME creation = {}, exit = {}, kernel = {}, user = {};
    if (!GetProcessTimes(attempt->info.hProcess, &creation, &exit, &kernel, &user)) {
        *error = GetLastError();
        TerminateProcess(attempt->info.hProcess, ERROR_INVALID_DATA);
        WaitForSingleObject(attempt->info.hProcess, 5000);
        if (attempt->info.hThread) CloseHandle(attempt->info.hThread);
        CloseHandle(attempt->info.hProcess);
        attempt->info = {};
        return false;
    }
    attempt->identity.pid = attempt->info.dwProcessId;
    attempt->identity.creationTime = FileTimeValue(creation);
    if (!attempt->identity.creationTime) {
        *error = ERROR_INVALID_DATA;
        TerminateProcess(attempt->info.hProcess, ERROR_INVALID_DATA);
        WaitForSingleObject(attempt->info.hProcess, 5000);
        if (attempt->info.hThread) CloseHandle(attempt->info.hThread);
        CloseHandle(attempt->info.hProcess);
        attempt->info = {};
        attempt->identity = {};
        return false;
    }
    result->processHandle = reinterpret_cast<uintptr_t>(attempt->info.hProcess);
    result->threadHandle = reinterpret_cast<uintptr_t>(attempt->info.hThread);
    result->identity = attempt->identity;
    return true;
}

bool IdentityStillMatches(void* context, const ProcessInformation*) {
    Attempt* attempt = static_cast<Attempt*>(context);
    return attempt && ChildIdentityStillMatches(attempt->info, attempt->identity);
}

bool PublishAndInject(void* context, const ProcessInformation*, uint32_t* error) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt || !error || !CreateChildMappings(attempt)) { if (error) *error = ERROR_ACCESS_DENIED; return false; }
    if (!RemoteLoadGuardDll(attempt)) { *error = GetLastError() ? GetLastError() : ERROR_DLL_INIT_FAILED; return false; }
    return true;
}

bool WaitChildReady(void* context, const ProcessInformation* process, uint32_t* error) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt || !process || !error || !attempt->mapping.readyEvent || !attempt->mapping.failedEvent) return false;
    HANDLE events[2] = {attempt->mapping.readyEvent, attempt->mapping.failedEvent};
    const DWORD wait = WaitForMultipleObjects(2, events, FALSE, kReadyTimeoutMs);
    if (wait != WAIT_OBJECT_0 || !IdentityStillMatches(context, process)) {
        MemoryBarrier();
        const uint32_t childFailure = attempt->mapping.status ? attempt->mapping.status->failureCode : 0;
        *error = wait == WAIT_OBJECT_0 + 1 && childFailure >= 1001 && childFailure <= 1012 ?
            childFailure : (wait == WAIT_TIMEOUT ? ERROR_TIMEOUT :
                (wait >= WAIT_ABANDONED_0 ? ERROR_INVALID_HANDLE : ERROR_PROCESS_ABORTED));
        return false;
    }
    MemoryBarrier();
    const StatusV1* status = attempt->mapping.status;
    if (!status || !attempt->mapping.config || !ValidateReadyStatus(*status, *attempt->mapping.config)) {
        *error = ERROR_INVALID_DATA;
        return false;
    }
    return true;
}

bool ResumeOwnedThread(void* context, const ProcessInformation*, uint32_t* error) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt || !attempt->info.hThread) { if (error) *error = ERROR_INVALID_HANDLE; return false; }
    const DWORD previousSuspendCount = ResumeThread(attempt->info.hThread);
    if (previousSuspendCount == static_cast<DWORD>(-1)) {
        if (error) *error = GetLastError();
        return false;
    }
    if (previousSuspendCount != 1) {
        if (error) *error = ERROR_INVALID_STATE;
        return false;
    }
    return true;
}

void TerminateOwnedChild(void* context, const ProcessInformation*) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt || !attempt->info.hProcess) return;
    TerminateProcess(attempt->info.hProcess, ERROR_PROCESS_ABORTED);
    WaitForSingleObject(attempt->info.hProcess, 5000);
}

void CloseReturnedHandles(void* context, const ProcessInformation*) {
    Attempt* attempt = static_cast<Attempt*>(context);
    if (!attempt) return;
    if (attempt->info.hThread) { CloseHandle(attempt->info.hThread); attempt->info.hThread = nullptr; }
    if (attempt->info.hProcess) { CloseHandle(attempt->info.hProcess); attempt->info.hProcess = nullptr; }
}

template <typename Invocation>
BOOL InvokeForBarrier(void* context, DWORD flags, LPPROCESS_INFORMATION info) {
    return (*static_cast<Invocation*>(context))(flags, info);
}

template <typename Invocation>
BOOL RunTargetBarrierWithOutput(Invocation& invocation, DWORD originalFlags, LPPROCESS_INFORMATION output) {
    if (!output) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    *output = {};
    Attempt attempt;
    attempt.invoke = &InvokeForBarrier<Invocation>;
    attempt.invokeContext = &invocation;
    LaunchCallbacks callbacks = {};
    callbacks.context = &attempt;
    callbacks.createSuspended = &CreateAndInject;
    callbacks.identityStillMatches = &IdentityStillMatches;
    callbacks.publishAndInject = &PublishAndInject;
    callbacks.waitPolicyReady = &WaitChildReady;
    callbacks.resumePrimaryThread = &ResumeOwnedThread;
    callbacks.terminateOwnedChild = &TerminateOwnedChild;
    callbacks.closeReturnedHandles = &CloseReturnedHandles;
    ProcessInformation resultInfo = {};
    const LaunchResult result = RunLaunchBarrier(callbacks, (originalFlags & CREATE_SUSPENDED) != 0, &resultInfo);
    if (!result.success) {
        SetLastError(result.win32Error ? result.win32Error : ERROR_GEN_FAILURE);
        return FALSE;
    }
    output->hProcess = attempt.info.hProcess;
    output->hThread = attempt.info.hThread;
    output->dwProcessId = attempt.info.dwProcessId;
    output->dwThreadId = attempt.info.dwThreadId;
    attempt.info = {};
    g_state.childLaunches.fetch_add(1, std::memory_order_relaxed);
    SetLastError(ERROR_SUCCESS);
    return TRUE;
}

BOOL WINAPI GuardCreateProcessW(LPCWSTR app, LPWSTR command, LPSECURITY_ATTRIBUTES processAttributes,
    LPSECURITY_ATTRIBUTES threadAttributes, BOOL inherit, DWORD flags, LPVOID environment,
    LPCWSTR currentDirectory, LPSTARTUPINFOW startup, LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessWFn>(g_state.requests[0].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(app, command, processAttributes, threadAttributes, inherit,
        flags, environment, currentDirectory, startup, processInformation);
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app, command, currentDirectory, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(app, command, processAttributes, threadAttributes, inherit, flags,
            environment, currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(app, command, processAttributes, threadAttributes, inherit,
            (flags | forcedFlags), environment, currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

BOOL WINAPI GuardCreateProcessA(LPCSTR app, LPSTR command, LPSECURITY_ATTRIBUTES processAttributes,
    LPSECURITY_ATTRIBUTES threadAttributes, BOOL inherit, DWORD flags, LPVOID environment,
    LPCSTR currentDirectory, LPSTARTUPINFOA startup, LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessAFn>(g_state.requests[1].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(app, command, processAttributes, threadAttributes, inherit,
        flags, environment, currentDirectory, startup, processInformation);
    const auto passThroughUnlessCandidate = [&]() {
        if (LooksLikeUnclassifiableAnsiAudioCandidate(app, command)) {
            SetLastError(ERROR_ACCESS_DENIED);
            return FALSE;
        }
        return original(app, command, processAttributes, threadAttributes, inherit, flags,
            environment, currentDirectory, startup, processInformation);
    };
    std::wstring wideApp, wideCommand, wideCwd;
    if (app && !ConvertAnsi(app, &wideApp)) return passThroughUnlessCandidate();
    if (command && !ConvertAnsi(command, &wideCommand, 32768)) return passThroughUnlessCandidate();
    if (currentDirectory && !ConvertAnsi(currentDirectory, &wideCwd)) return passThroughUnlessCandidate();
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app ? wideApp.c_str() : nullptr,
        command ? const_cast<LPWSTR>(wideCommand.c_str()) : nullptr,
        currentDirectory ? wideCwd.c_str() : nullptr, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(app, command, processAttributes, threadAttributes, inherit, flags,
            environment, currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(app, command, processAttributes, threadAttributes, inherit,
            (flags | forcedFlags), environment, currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

BOOL WINAPI GuardCreateProcessAsUserW(HANDLE token, LPCWSTR app, LPWSTR command,
    LPSECURITY_ATTRIBUTES processAttributes, LPSECURITY_ATTRIBUTES threadAttributes, BOOL inherit,
    DWORD flags, LPVOID environment, LPCWSTR currentDirectory, LPSTARTUPINFOW startup,
    LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessAsUserWFn>(g_state.requests[2].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(token, app, command, processAttributes, threadAttributes,
        inherit, flags, environment, currentDirectory, startup, processInformation);
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app, command, currentDirectory, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(token, app, command, processAttributes, threadAttributes, inherit,
            flags, environment, currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(token, app, command, processAttributes, threadAttributes, inherit,
            (flags | forcedFlags), environment, currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

BOOL WINAPI GuardCreateProcessAsUserA(HANDLE token, LPCSTR app, LPSTR command,
    LPSECURITY_ATTRIBUTES processAttributes, LPSECURITY_ATTRIBUTES threadAttributes, BOOL inherit,
    DWORD flags, LPVOID environment, LPCSTR currentDirectory, LPSTARTUPINFOA startup,
    LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessAsUserAFn>(g_state.requests[3].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(token, app, command, processAttributes, threadAttributes,
        inherit, flags, environment, currentDirectory, startup, processInformation);
    const auto passThroughUnlessCandidate = [&]() {
        if (LooksLikeUnclassifiableAnsiAudioCandidate(app, command)) {
            SetLastError(ERROR_ACCESS_DENIED);
            return FALSE;
        }
        return original(token, app, command, processAttributes, threadAttributes, inherit, flags,
            environment, currentDirectory, startup, processInformation);
    };
    std::wstring wideApp, wideCommand, wideCwd;
    if (app && !ConvertAnsi(app, &wideApp)) return passThroughUnlessCandidate();
    if (command && !ConvertAnsi(command, &wideCommand, 32768)) return passThroughUnlessCandidate();
    if (currentDirectory && !ConvertAnsi(currentDirectory, &wideCwd)) return passThroughUnlessCandidate();
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app ? wideApp.c_str() : nullptr,
        command ? const_cast<LPWSTR>(wideCommand.c_str()) : nullptr,
        currentDirectory ? wideCwd.c_str() : nullptr, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(token, app, command, processAttributes, threadAttributes, inherit,
            flags, environment, currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(token, app, command, processAttributes, threadAttributes, inherit,
            (flags | forcedFlags), environment, currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

BOOL WINAPI GuardCreateProcessWithTokenW(HANDLE token, DWORD logonFlags, LPCWSTR app, LPWSTR command,
    DWORD flags, LPVOID environment, LPCWSTR currentDirectory, LPSTARTUPINFOW startup,
    LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessWithTokenWFn>(g_state.requests[4].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(token, logonFlags, app, command, flags, environment,
        currentDirectory, startup, processInformation);
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app, command, currentDirectory, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(token, logonFlags, app, command, flags, environment,
            currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(token, logonFlags, app, command, (flags | forcedFlags), environment,
            currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

BOOL WINAPI GuardCreateProcessWithLogonW(LPCWSTR user, LPCWSTR domain, LPCWSTR password, DWORD logonFlags,
    LPCWSTR app, LPWSTR command, DWORD flags, LPVOID environment, LPCWSTR currentDirectory,
    LPSTARTUPINFOW startup, LPPROCESS_INFORMATION processInformation) {
    const auto original = reinterpret_cast<CreateProcessWithLogonWFn>(g_state.requests[5].original);
    if (!original) { SetLastError(ERROR_PROC_NOT_FOUND); return FALSE; }
    if (g_parentHookDepth) return original(user, domain, password, logonFlags, app, command, flags,
        environment, currentDirectory, startup, processInformation);
    bool ambiguous = false;
    const bool targeted = IsTargetLaunch(app, command, currentDirectory, &ambiguous);
    if (!targeted) {
        if (ambiguous) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
        ++g_parentHookDepth;
        const BOOL result = original(user, domain, password, logonFlags, app, command, flags,
            environment, currentDirectory, startup, processInformation);
        --g_parentHookDepth;
        return result;
    }
    if (!processInformation) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto invocation = [&](DWORD forcedFlags, LPPROCESS_INFORMATION info) {
        ++g_parentHookDepth;
        const BOOL result = original(user, domain, password, logonFlags, app, command, (flags | forcedFlags),
            environment, currentDirectory, startup, info);
        --g_parentHookDepth;
        return result;
    };
    return RunTargetBarrierWithOutput(invocation, flags, processInformation);
}

bool MakeAbsoluteExisting(const std::wstring& input, std::wstring* output) {
    if (!BoundedString(input, kMaxImagePathChars) || !IsAbsolutePath(input) || !output) return false;
    DWORD size = GetFullPathNameW(input.c_str(), 0, nullptr, nullptr);
    if (!size || size > kMaxImagePathChars) return false;
    std::vector<wchar_t> path(size + 1);
    const DWORD written = GetFullPathNameW(input.c_str(), static_cast<DWORD>(path.size()), path.data(), nullptr);
    if (!written || written >= path.size()) return false;
    DWORD attrs = GetFileAttributesW(path.data());
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) return false;
    output->assign(path.data(), written);
    return true;
}

} // namespace

bool ParentLaunchGuardInstalled() { return g_state.installed.load(std::memory_order_acquire); }
uint64_t ParentGuardGeneration() { return g_state.options.hostGeneration; }

bool InstallParentLaunchGuard(const ParentOptions& options, FailureCode* failure) {
    if (failure) *failure = FailureCode::config_invalid;
    while (g_installLock.test_and_set(std::memory_order_acquire)) Sleep(0);
    std::wstring qqImage, dllPath, parentImage;
    if (!MakeAbsoluteExisting(options.qqImagePath, &qqImage) || !MakeAbsoluteExisting(options.guardDllPath, &dllPath) ||
        options.qqBuild != kSupportedQqBuild || !BoundedString(options.captureEndpointId, kMaxEndpointIdChars) ||
        !BoundedString(options.renderEndpointId, kMaxEndpointIdChars) ||
        options.captureEndpointId == options.renderEndpointId || !options.hostGeneration) {
        g_installLock.clear(std::memory_order_release);
        return false;
    }
    if (options.captureEndpointId.size() >= kMaxEndpointIdChars ||
        options.renderEndpointId.size() >= kMaxEndpointIdChars) {
        g_installLock.clear(std::memory_order_release);
        return false;
    }
    ParentOptions normalized = options;
    normalized.qqImagePath = qqImage;
    normalized.guardDllPath = dllPath;
    if (g_state.installed.load(std::memory_order_relaxed)) {
        const bool same = SameParentPolicy(normalized, g_state.options);
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = same ? FailureCode::none : FailureCode::config_invalid;
        return same;
    }
    if (g_state.hooksCommitted.load(std::memory_order_relaxed)) {
        const bool same = SameParentPolicy(normalized, g_state.options);
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = same ? g_state.terminalFailure : FailureCode::config_invalid;
        return false;
    }
    if (!GetImagePath(GetCurrentProcess(), &parentImage) || !EqualPath(parentImage, qqImage)) {
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::wrong_image_or_command;
        return false;
    }
    g_state.parentImage = parentImage;
    g_state.parentPid = GetCurrentProcessId();
    FILETIME exit = {}, kernel = {}, user = {};
    if (!GetProcessTimes(GetCurrentProcess(), &g_state.parentCreation, &exit, &kernel, &user) ||
        !GetTokenSid(GetCurrentProcess(), &g_state.sidStorage, &g_state.actorSid)) {
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::actor_mismatch;
        return false;
    }
    if (HasExistingQqChild()) {
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::existing_candidate_child;
        return false;
    }

    g_state.options = normalized;

    const HookRequest requested[] = {
        {L"KernelBase.dll", "CreateProcessW", reinterpret_cast<void*>(&GuardCreateProcessW)},
        {L"KernelBase.dll", "CreateProcessA", reinterpret_cast<void*>(&GuardCreateProcessA)},
        {L"advapi32.dll", "CreateProcessAsUserW", reinterpret_cast<void*>(&GuardCreateProcessAsUserW)},
        {L"advapi32.dll", "CreateProcessAsUserA", reinterpret_cast<void*>(&GuardCreateProcessAsUserA)},
        {L"advapi32.dll", "CreateProcessWithTokenW", reinterpret_cast<void*>(&GuardCreateProcessWithTokenW)},
        {L"advapi32.dll", "CreateProcessWithLogonW", reinterpret_cast<void*>(&GuardCreateProcessWithLogonW)},
    };
    for (size_t i = 0; i < ARRAYSIZE(requested); ++i) g_state.requests[i] = requested[i];
    HMODULE pinnedModule = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                            reinterpret_cast<LPCWSTR>(&InstallParentLaunchGuard), &pinnedModule)) {
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::hook_install_failed;
        return false;
    }
    g_state.backend = CreateMinHookBackend();
    const bool hookOk = g_state.backend && InstallHookSet(g_state.backend, g_state.requests, ARRAYSIZE(g_state.requests));
    if (!hookOk) {
        if (g_state.backend) { DestroyMinHookBackend(g_state.backend); g_state.backend = nullptr; }
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::hook_install_failed;
        return false;
    }
    g_state.hooksCommitted.store(true, std::memory_order_release);
    // Catch a candidate that became visible while the hook transaction was committed.
    if (HasExistingQqChild()) {
        // Keep committed hooks active so no later candidate can launch without
        // the barrier. Installation still fails because an existing candidate's
        // startup timing cannot be proven.
        g_state.terminalFailure = FailureCode::existing_candidate_child;
        g_installLock.clear(std::memory_order_release);
        if (failure) *failure = FailureCode::existing_candidate_child;
        return false;
    }
    g_state.installed.store(true, std::memory_order_release);
    g_installLock.clear(std::memory_order_release);
    if (failure) *failure = FailureCode::none;
    return true;
}

} // namespace qar
