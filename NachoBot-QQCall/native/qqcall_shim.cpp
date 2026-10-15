// Minimal x64 in-process QQ Kernel bootstrap shim. This file intentionally has
// no Windows SDK dependency and no CRT dependency.

typedef unsigned char BYTE;
typedef unsigned short WORD;
typedef unsigned long DWORD;
typedef long LONG;
typedef unsigned long long ULONGLONG;
typedef unsigned long long SIZE_T;
typedef int BOOL;
typedef void* HANDLE;
typedef void* HMODULE;
typedef void* HINSTANCE;
typedef void* FARPROC;
typedef void* LPVOID;
typedef const void* LPCVOID;
typedef wchar_t WCHAR;
typedef const WCHAR* LPCWSTR;
typedef WCHAR* LPWSTR;

#define WINAPI __stdcall
#define TRUE 1
#define FALSE 0
#define INVALID_HANDLE_VALUE ((HANDLE)(LONG_PTR)-1)
#define LONG_PTR long long
#define PAGE_READWRITE 0x04
#define PAGE_EXECUTE_READWRITE 0x40
#define GENERIC_WRITE 0x40000000UL
#define CREATE_ALWAYS 2UL
#define FILE_ATTRIBUTE_NORMAL 0x80UL
#define FILE_SHARE_READ 0x00000001UL
#define FILE_SHARE_WRITE 0x00000002UL
#define TH32CS_SNAPMODULE 0x00000008UL
#define TH32CS_SNAPMODULE32 0x00000010UL
#define CP_UTF8 65001UL
#define WC_ERR_INVALID_CHARS 0x00000080UL
#define DLL_PROCESS_ATTACH 1UL

extern "C" __declspec(dllimport) BOOL WINAPI CloseHandle(HANDLE);
extern "C" __declspec(dllimport) HANDLE WINAPI CreateFileW(LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE);
extern "C" __declspec(dllimport) HANDLE WINAPI CreateToolhelp32Snapshot(DWORD, DWORD);
extern "C" __declspec(dllimport) BOOL WINAPI DisableThreadLibraryCalls(HINSTANCE);
extern "C" __declspec(dllimport) DWORD WINAPI GetCurrentProcessId(void);
extern "C" __declspec(dllimport) DWORD WINAPI GetLastError(void);
extern "C" __declspec(dllimport) DWORD WINAPI GetPrivateProfileStringW(LPCWSTR, LPCWSTR, LPCWSTR, LPWSTR, DWORD, LPCWSTR);
extern "C" LONG __cdecl _InterlockedCompareExchange(volatile LONG*, LONG, LONG);
extern "C" void* __cdecl _InterlockedCompareExchangePointer(void* volatile*, void*, void*);
extern "C" LONG __cdecl _InterlockedExchange(volatile LONG*, LONG);
#pragma intrinsic(_InterlockedCompareExchange, _InterlockedCompareExchangePointer, _InterlockedExchange)
#define InterlockedCompareExchange _InterlockedCompareExchange
#define InterlockedCompareExchangePointer _InterlockedCompareExchangePointer
#define InterlockedExchange _InterlockedExchange
extern "C" __declspec(dllimport) BOOL WINAPI Module32FirstW(HANDLE, void*);
extern "C" __declspec(dllimport) BOOL WINAPI Module32NextW(HANDLE, void*);
extern "C" __declspec(dllimport) void WINAPI Sleep(DWORD);
extern "C" __declspec(dllimport) BOOL WINAPI VirtualProtect(LPVOID, SIZE_T, DWORD, DWORD*);
extern "C" __declspec(dllimport) int WINAPI WideCharToMultiByte(DWORD, DWORD, LPCWSTR, int, char*, int, const char*, BOOL*);
extern "C" __declspec(dllimport) BOOL WINAPI WriteFile(HANDLE, LPCVOID, DWORD, DWORD*, LPVOID);

typedef int napi_status;
typedef void* napi_env;
typedef void* napi_value;
typedef void* napi_callback_info;
typedef napi_status (WINAPI *napi_get_cb_info_fn)(napi_env, napi_callback_info, SIZE_T*, napi_value*, napi_value*, void**);
typedef napi_status (WINAPI *napi_create_string_utf8_fn)(napi_env, const char*, SIZE_T, napi_value*);
typedef napi_status (WINAPI *napi_run_script_fn)(napi_env, napi_value, napi_value*);
typedef napi_status (WINAPI *napi_is_exception_pending_fn)(napi_env, bool*);
typedef napi_status (WINAPI *napi_get_and_clear_last_exception_fn)(napi_env, napi_value*);
typedef napi_status (WINAPI *napi_get_value_int32_fn)(napi_env, napi_value, int*);

extern "C" __declspec(dllimport) FARPROC WINAPI GetProcAddress(HMODULE, const char*);

struct MODULEENTRY32W_MIN {
    DWORD dwSize;
    DWORD th32ModuleID;
    DWORD th32ProcessID;
    DWORD GlblcntUsage;
    DWORD ProccntUsage;
    BYTE* modBaseAddr;
    DWORD modBaseSize;
    HMODULE hModule;
    WCHAR szModule[256];
    WCHAR szExePath[260];
};

struct ShimConfig {
    WCHAR modulePath[4096];
    WCHAR wrapperModulePath[4096];
    WCHAR pipeName[512];
    WCHAR appPath[4096];
    WCHAR sessionName[512];
    WCHAR reportPath[4096];
    WCHAR ipcToken[1024];
    ULONGLONG connectionGeneration;
};

struct QQCallShimStatusSnapshot {
    DWORD size;
    DWORD version;
    DWORD flags;
    DWORD errorCode;
    DWORD processId;
    DWORD reserved;
    ULONGLONG connectionGeneration;
};

enum ShimError {
    ERR_NONE = 0,
    ERR_CONFIG_PATH = 10,
    ERR_CONFIG_MISSING = 11,
    ERR_CONFIG_INVALID = 12,
    ERR_CONFIG_BINDING_MISMATCH = 13,
    ERR_CONTROLLER_BUSY = 14,
    ERR_WRAPPER_AMBIGUOUS = 20,
    ERR_WRAPPER_NOT_FOUND = 21,
    ERR_QQNT_NOT_FOUND = 22,
    ERR_IMPORT_LAYOUT = 23,
    ERR_IMPORT_NOT_FOUND = 24,
    ERR_IMPORT_ALREADY_HOOKED = 25,
    ERR_NAPI_EXPORT_MISSING = 26,
    ERR_IAT_PROTECT = 27,
    ERR_IAT_CAS = 28,
    ERR_IAT_OWNERSHIP_LOST = 29,
    ERR_CANCEL_TOO_LATE = 30,
    ERR_CANCEL_NOT_ARMED = 31,
    ERR_EXCEPTION_API = 40,
    ERR_PREEXISTING_EXCEPTION = 41,
    ERR_CREATE_STRING = 42,
    ERR_RUN_SCRIPT = 43,
    ERR_SCRIPT_STATUS = 44,
    ERR_CLEAR_EXCEPTION = 45,
    ERR_LOADER_UNAVAILABLE = 46,
    ERR_SETIMMEDIATE_UNAVAILABLE = 47,
    ERR_CANCELLED = 48,
    ERR_STATUS_POINTER = 50
};

enum ShimFlags {
    FLAG_CAPABILITY_SCAN = 0x00000001,
    FLAG_IAT_ARMED = 0x00000002,
    FLAG_BOOTSTRAP_SCHEDULED = 0x00000004,
    FLAG_IAT_RESTORED = 0x00000008,
    FLAG_CANCELLED = 0x00000010,
    FLAG_LOADER_AVAILABLE = 0x00000020,
    FLAG_AGENT_OUTCOME_PENDING = 0x00000040,
    FLAG_JS_EXCEPTION_CLEARED = 0x00000080,
    FLAG_CALLBACK_CAPTURED = 0x00000100,
    FLAG_SCRIPT_FAILED = 0x00000200
};

static const DWORD MODULEENTRY32W_MIN_SIZE = 1080;
static const DWORD IMAGE_DOS_SIGNATURE = 0x5A4D;
static const DWORD IMAGE_NT_SIGNATURE = 0x00004550;
static const WORD IMAGE_FILE_MACHINE_AMD64 = 0x8664;
static const WORD IMAGE_NT_OPTIONAL_HDR64_MAGIC = 0x20B;
static const ULONGLONG IMAGE_ORDINAL_FLAG64 = 0x8000000000000000ULL;
static const DWORD IMAGE_DIRECTORY_ENTRY_IMPORT = 1;
static const DWORD PAGE_SIZE_HINT = 0x1000;

static volatile LONG g_boundState = 0; // 0 unbound, 1 loading config, 2 bound
static volatile LONG g_configLock = 0;
static volatile LONG g_startLock = 0;
static volatile LONG g_transitionLock = 0;
static volatile LONG g_reportLock = 0;
static volatile LONG g_callbackClaimed = 0;
static volatile LONG g_cancelled = 0;
static volatile LONG g_flags = 0;
static volatile LONG g_errorCode = ERR_NONE;
static ShimConfig g_config;
static BYTE* g_wrapperBase = 0;
static DWORD g_wrapperSize = 0;
static BYTE* g_qqntBase = 0;
static DWORD g_qqntSize = 0;
static void** g_iatSlot = 0;
static void* g_originalPointer = 0;
static napi_get_cb_info_fn g_original = 0;
static napi_create_string_utf8_fn g_createString = 0;
static napi_run_script_fn g_runScript = 0;
static napi_is_exception_pending_fn g_isExceptionPending = 0;
static napi_get_and_clear_last_exception_fn g_clearException = 0;
static napi_get_value_int32_fn g_getInt32 = 0;
static char g_script[32768];
static char g_utf8Scratch[16384];
static WCHAR g_wrapperPathScratch[4096];
static WCHAR g_reportPathScratch[4200];
static ShimConfig g_configScratch;

#if defined(QQCALL_SHIM_TEST_HOOKS)
static volatile LONG g_testStartOrdinal = 0;
static volatile LONG g_testPauseOrdinal = 0;
static volatile LONG g_testStartPaused = 0;
static volatile LONG g_testStartReleased = 0;

static void TestPauseAfterStartFastPath(void) {
    LONG previous;
    do {
        previous = InterlockedCompareExchange(&g_testStartOrdinal, 0, 0);
    } while (InterlockedCompareExchange(&g_testStartOrdinal, previous + 1, previous) != previous);
    if (previous + 1 != InterlockedCompareExchange(&g_testPauseOrdinal, 0, 0)) return;
    InterlockedExchange(&g_testStartPaused, 1);
    while (InterlockedCompareExchange(&g_testStartReleased, 0, 0) == 0) Sleep(1);
}
#endif

static DWORD WINAPI QQCallShimNapiGetCbInfo(napi_env, napi_callback_info, SIZE_T*, napi_value*, napi_value*, void**);

static void Lock(volatile LONG* lock) {
    while (InterlockedCompareExchange(lock, 1, 0) != 0) Sleep(0);
}

static void Unlock(volatile LONG* lock) {
    InterlockedExchange(lock, 0);
}

static void SetFlag(DWORD flag) {
    LONG oldValue;
    do {
        oldValue = InterlockedCompareExchange(&g_flags, 0, 0);
    } while (InterlockedCompareExchange(&g_flags, oldValue | (LONG)flag, oldValue) != oldValue);
}

static void ClearFlag(DWORD flag) {
    LONG oldValue;
    do {
        oldValue = InterlockedCompareExchange(&g_flags, 0, 0);
    } while (InterlockedCompareExchange(&g_flags, oldValue & ~(LONG)flag, oldValue) != oldValue);
}

static void SetError(DWORD code) {
    InterlockedExchange(&g_errorCode, (LONG)code);
}

static DWORD ReadValue(LPCWSTR iniPath, LPCWSTR key, WCHAR* output, DWORD capacity) {
    if (!output || capacity < 2) return 0;
    output[0] = 0;
    DWORD copied = GetPrivateProfileStringW(L"QQCallShim", key, L"", output, capacity, iniPath);
    if (copied == 0 || copied >= capacity - 1) return 0;
    return copied;
}

static BOOL HasLineBreak(const WCHAR* s) {
    if (!s) return TRUE;
    for (DWORD i = 0; s[i]; ++i) if (s[i] == 10 || s[i] == 13) return TRUE;
    return FALSE;
}

static BOOL IsAbsolutePath(const WCHAR* s) {
    if (!s || !s[0] || HasLineBreak(s)) return FALSE;
    if (((s[0] >= L'A' && s[0] <= L'Z') || (s[0] >= L'a' && s[0] <= L'z')) && s[1] == L':' && (s[2] == L'\\' || s[2] == L'/')) return TRUE;
    if (s[0] == L'\\' && s[1] == L'\\') return TRUE;
    return FALSE;
}

static BOOL ParseGeneration(const WCHAR* s, ULONGLONG* value) {
    if (!s || !*s || !value) return FALSE;
    ULONGLONG result = 0;
    for (DWORD i = 0; s[i]; ++i) {
        if (s[i] < L'0' || s[i] > L'9') return FALSE;
        DWORD digit = (DWORD)(s[i] - L'0');
        if (result > 900719925474099ULL) return FALSE; // keep JavaScript integer exact
        result = result * 10ULL + digit;
    }
    if (result == 0 || result > 9007199254740991ULL) return FALSE;
    *value = result;
    return TRUE;
}

static BOOL ValidToken(const WCHAR* token) {
    DWORD length = 0;
    if (!token) return FALSE;
    for (; token[length]; ++length) {
        WCHAR c = token[length];
        if (!((c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z') || (c >= L'0' && c <= L'9') || c == L'_' || c == L'-')) return FALSE;
    }
    return length >= 43 && length <= 128;
}

static BOOL ValidPipeName(const WCHAR* pipeName) {
    if (!pipeName) return FALSE;
    const WCHAR prefix[] = L"\\\\.\\pipe\\nachobot-qqcall-";
    DWORD i = 0;
    for (; prefix[i]; ++i) if (pipeName[i] != prefix[i]) return FALSE;
    DWORD pid = GetCurrentProcessId();
    WCHAR digits[16];
    DWORD count = 0;
    do { digits[count++] = (WCHAR)(L'0' + (pid % 10)); pid /= 10; } while (pid && count < 16);
    for (DWORD j = 0; j < count; ++j) if (pipeName[i++] != digits[count - j - 1]) return FALSE;
    if (pipeName[i++] != L'-') return FALSE;
    DWORD nonceLength = 0;
    for (; pipeName[i]; ++i, ++nonceLength) {
        WCHAR c = pipeName[i];
        if (!((c >= L'a' && c <= L'f') || (c >= L'A' && c <= L'F') || (c >= L'0' && c <= L'9'))) return FALSE;
    }
    return nonceLength >= 16 && nonceLength <= 64;
}

static BOOL ReadConfig(LPCWSTR iniPath, ShimConfig* cfg) {
    if (!iniPath || !cfg || !IsAbsolutePath(iniPath)) return FALSE;
    if (!ReadValue(iniPath, L"modulePath", cfg->modulePath, 4096) || !IsAbsolutePath(cfg->modulePath)) return FALSE;
    if (!ReadValue(iniPath, L"pipeName", cfg->pipeName, 512) || !ValidPipeName(cfg->pipeName)) return FALSE;
    if (!ReadValue(iniPath, L"appPath", cfg->appPath, 4096) || !IsAbsolutePath(cfg->appPath)) return FALSE;
    if (!ReadValue(iniPath, L"sessionName", cfg->sessionName, 81)) return FALSE;
    DWORD sessionLength = 0;
    for (; cfg->sessionName[sessionLength]; ++sessionLength) {
        WCHAR c = cfg->sessionName[sessionLength];
        if (!((c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z') || (c >= L'0' && c <= L'9') || c == L'_' || c == L'.' || c == L'-')) return FALSE;
    }
    if (sessionLength == 0 || sessionLength > 80) return FALSE;
    if (!ReadValue(iniPath, L"reportPath", cfg->reportPath, 4096) || !IsAbsolutePath(cfg->reportPath)) return FALSE;
    if (!ReadValue(iniPath, L"ipcToken", cfg->ipcToken, 1024) || !ValidToken(cfg->ipcToken)) return FALSE;
    WCHAR generation[32];
    if (!ReadValue(iniPath, L"connectionGeneration", generation, 32) || !ParseGeneration(generation, &cfg->connectionGeneration)) return FALSE;
    return TRUE;
}

static BOOL SameConfig(const ShimConfig* a, const ShimConfig* b) {
    if (!a || !b || a->connectionGeneration != b->connectionGeneration) return FALSE;
    for (DWORD i = 0; a->modulePath[i] || b->modulePath[i]; ++i) if (a->modulePath[i] != b->modulePath[i]) return FALSE;
    for (DWORD i = 0; a->pipeName[i] || b->pipeName[i]; ++i) if (a->pipeName[i] != b->pipeName[i]) return FALSE;
    for (DWORD i = 0; a->appPath[i] || b->appPath[i]; ++i) if (a->appPath[i] != b->appPath[i]) return FALSE;
    for (DWORD i = 0; a->sessionName[i] || b->sessionName[i]; ++i) if (a->sessionName[i] != b->sessionName[i]) return FALSE;
    for (DWORD i = 0; a->reportPath[i] || b->reportPath[i]; ++i) if (a->reportPath[i] != b->reportPath[i]) return FALSE;
    for (DWORD i = 0; a->ipcToken[i] || b->ipcToken[i]; ++i) if (a->ipcToken[i] != b->ipcToken[i]) return FALSE;
    return TRUE;
}

static void CopyWide(WCHAR* destination, const WCHAR* source, DWORD capacity) {
    DWORD i = 0;
    for (; i + 1 < capacity && source[i]; ++i) destination[i] = source[i];
    destination[i] = 0;
}

static void CopyConfig(ShimConfig* destination, const ShimConfig* source) {
    CopyWide(destination->modulePath, source->modulePath, 4096);
    CopyWide(destination->wrapperModulePath, source->wrapperModulePath, 4096);
    CopyWide(destination->pipeName, source->pipeName, 512);
    CopyWide(destination->appPath, source->appPath, 4096);
    CopyWide(destination->sessionName, source->sessionName, 512);
    CopyWide(destination->reportPath, source->reportPath, 4096);
    CopyWide(destination->ipcToken, source->ipcToken, 1024);
    destination->connectionGeneration = source->connectionGeneration;
}

static BOOL BindConfig(LPCWSTR iniPath) {
    Lock(&g_configLock);
    if (!ReadConfig(iniPath, &g_configScratch)) {
        Unlock(&g_configLock); SetError(ERR_CONFIG_INVALID); return FALSE;
    }
    LONG state = InterlockedCompareExchange(&g_boundState, 1, 0);
    if (state == 0) {
        CopyConfig(&g_config, &g_configScratch);
        InterlockedExchange(&g_boundState, 2);
        Unlock(&g_configLock);
        return TRUE;
    }
    if (state == 1) { Unlock(&g_configLock); SetError(ERR_CONTROLLER_BUSY); return FALSE; }
    if (!SameConfig(&g_config, &g_configScratch)) { Unlock(&g_configLock); SetError(ERR_CONFIG_BINDING_MISMATCH); return FALSE; }
    Unlock(&g_configLock);
    return TRUE;
}

static BOOL EqualsAsciiNoCase(const WCHAR* wide, const char* ascii) {
    DWORD i = 0;
    if (!wide || !ascii) return FALSE;
    for (; ascii[i]; ++i) {
        WCHAR c = wide[i];
        if (c >= L'A' && c <= L'Z') c = (WCHAR)(c + (L'a' - L'A'));
        char a = ascii[i];
        if (a >= 'A' && a <= 'Z') a = (char)(a + ('a' - 'A'));
        if (c != (WCHAR)a) return FALSE;
    }
    return wide[i] == 0;
}

static BOOL FindModules(BYTE** wrapperBase, DWORD* wrapperSize, WCHAR* wrapperPath, DWORD wrapperPathCap, BYTE** qqntBase, DWORD* qqntSize) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (!snapshot || snapshot == INVALID_HANDLE_VALUE) return FALSE;
    MODULEENTRY32W_MIN entry;
    entry.dwSize = MODULEENTRY32W_MIN_SIZE;
    BOOL foundWrapper = FALSE;
    BOOL foundQqnt = FALSE;
    BOOL ambiguousWrapper = FALSE;
    if (Module32FirstW(snapshot, &entry)) {
        do {
            if (EqualsAsciiNoCase(entry.szModule, "wrapper.node")) {
                if (foundWrapper) ambiguousWrapper = TRUE;
                else {
                    foundWrapper = TRUE;
                    *wrapperBase = entry.modBaseAddr;
                    *wrapperSize = entry.modBaseSize;
                    DWORD i = 0;
                    for (; i + 1 < wrapperPathCap && entry.szExePath[i]; ++i) wrapperPath[i] = entry.szExePath[i];
                    wrapperPath[i] = 0;
                    if (entry.szExePath[i]) ambiguousWrapper = TRUE;
                }
            } else if (EqualsAsciiNoCase(entry.szModule, "QQNT.dll")) {
                if (foundQqnt) { *qqntBase = 0; *qqntSize = 0; }
                else {
                    foundQqnt = TRUE;
                    *qqntBase = entry.modBaseAddr;
                    *qqntSize = entry.modBaseSize;
                }
            }
            entry.dwSize = MODULEENTRY32W_MIN_SIZE;
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    if (ambiguousWrapper) { SetError(ERR_WRAPPER_AMBIGUOUS); return FALSE; }
    if (!foundWrapper) { SetError(ERR_WRAPPER_NOT_FOUND); return FALSE; }
    if (!foundQqnt || !*qqntBase) { SetError(ERR_QQNT_NOT_FOUND); return FALSE; }
    return TRUE;
}

static BOOL RangeOk(DWORD imageSize, DWORD rva, DWORD bytes) {
    if (rva >= imageSize || bytes > imageSize) return FALSE;
    return rva <= imageSize - bytes;
}

static DWORD Read32(const BYTE* p) {
    return (DWORD)p[0] | ((DWORD)p[1] << 8) | ((DWORD)p[2] << 16) | ((DWORD)p[3] << 24);
}

static WORD Read16(const BYTE* p) {
    return (WORD)(p[0] | ((WORD)p[1] << 8));
}

static ULONGLONG Read64(const BYTE* p) {
    return (ULONGLONG)Read32(p) | ((ULONGLONG)Read32(p + 4) << 32);
}

static BOOL ImageInfo(BYTE* base, DWORD mappedSize, DWORD* imageSize, DWORD* importRva, DWORD* importSize) {
    if (!base || mappedSize < 0x1000 || !imageSize || !importRva || !importSize) return FALSE;
    if (Read16(base) != IMAGE_DOS_SIGNATURE) return FALSE;
    DWORD lfanew = Read32(base + 0x3c);
    if (lfanew > mappedSize - 24 || Read32(base + lfanew) != IMAGE_NT_SIGNATURE) return FALSE;
    BYTE* file = base + lfanew + 4;
    if (Read16(file) != IMAGE_FILE_MACHINE_AMD64) return FALSE;
    WORD optionalSize = Read16(file + 16);
    BYTE* optional = file + 20;
    if (optionalSize < 240 || optional + optionalSize > base + mappedSize || Read16(optional) != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return FALSE;
    DWORD actualImageSize = Read32(optional + 56);
    if (actualImageSize == 0 || actualImageSize > mappedSize) return FALSE;
    DWORD directoryCount = Read32(optional + 108);
    if (directoryCount <= IMAGE_DIRECTORY_ENTRY_IMPORT) return FALSE;
    DWORD dir = 112 + IMAGE_DIRECTORY_ENTRY_IMPORT * 8;
    DWORD rva = Read32(optional + dir);
    DWORD size = Read32(optional + dir + 4);
    if (!rva || size < 20 || !RangeOk(actualImageSize, rva, size)) return FALSE;
    *imageSize = actualImageSize;
    *importRva = rva;
    *importSize = size;
    return TRUE;
}

static BOOL ImageImportSlot(BYTE* base, DWORD mappedSize, DWORD* slotRva) {
    DWORD imageSize = 0, importRva = 0, importSize = 0;
    if (!ImageInfo(base, mappedSize, &imageSize, &importRva, &importSize)) return FALSE;
    DWORD importEnd = importRva + importSize;
    BOOL found = FALSE;
    BOOL descriptorTerminated = FALSE;
    for (DWORD descriptorRva = importRva; descriptorRva + 20 <= importEnd; descriptorRva += 20) {
        BYTE* descriptor = base + descriptorRva;
        DWORD oft = Read32(descriptor);
        DWORD nameRva = Read32(descriptor + 12);
        DWORD firstThunk = Read32(descriptor + 16);
        if (!oft && !nameRva && !firstThunk) { descriptorTerminated = TRUE; break; }
        if (!nameRva || !firstThunk || !RangeOk(imageSize, nameRva, 1)) return FALSE;
        const char* dllName = (const char*)(base + nameRva);
        DWORD maxName = imageSize - nameRva;
        BOOL dllMatch = TRUE;
        const char target[] = "qqnt.dll";
        DWORD i = 0;
        for (; target[i]; ++i) {
            if (i >= maxName) return FALSE;
            char c = dllName[i];
            if (c >= 'A' && c <= 'Z') c = (char)(c + ('a' - 'A'));
            if (c != target[i]) { dllMatch = FALSE; break; }
        }
        if (!dllMatch || (i < maxName && dllName[i] != 0)) continue;
        if (!oft) return FALSE; // the bound FirstThunk no longer carries import names
        DWORD namesRva = oft;
        if (!RangeOk(imageSize, namesRva, 8) || !RangeOk(imageSize, firstThunk, 8)) return FALSE;
        DWORD maximum = (imageSize - namesRva) / 8;
        if ((imageSize - firstThunk) / 8 < maximum) maximum = (imageSize - firstThunk) / 8;
        BOOL thunkTerminated = FALSE;
        for (DWORD index = 0; index < maximum; ++index) {
            DWORD thunkRva = namesRva + index * 8;
            ULONGLONG thunk = Read64(base + thunkRva);
            if (thunk == 0) { thunkTerminated = TRUE; break; }
            if (thunk & IMAGE_ORDINAL_FLAG64) continue;
            if (thunk > 0xffffffffULL || !RangeOk(imageSize, (DWORD)thunk, 3)) return FALSE;
            const char* importName = (const char*)(base + (DWORD)thunk + 2);
            const char targetName[] = "napi_get_cb_info";
            BOOL match = TRUE;
            for (DWORD n = 0; targetName[n]; ++n) {
                if (!RangeOk(imageSize, (DWORD)thunk + 2 + n, 1)) return FALSE;
                if (importName[n] != targetName[n]) { match = FALSE; break; }
            }
            if (match) {
                DWORD nameLength = 0;
                while (targetName[nameLength]) ++nameLength;
                if (!RangeOk(imageSize, (DWORD)thunk + 2 + nameLength, 1) || importName[nameLength] != 0) match = FALSE;
            }
            if (match) {
                ULONGLONG slotRvaWide = (ULONGLONG)firstThunk + (ULONGLONG)index * 8ULL;
                if (slotRvaWide > 0xffffffffULL || !RangeOk(imageSize, (DWORD)slotRvaWide, 8)) return FALSE;
                if (((ULONGLONG)(base + (DWORD)slotRvaWide) & 7ULL) != 0) return FALSE;
                if (found) return FALSE;
                *slotRva = (DWORD)slotRvaWide;
                found = TRUE;
            }
        }
        if (!thunkTerminated) return FALSE;
    }
    return descriptorTerminated && found;
}

static BOOL Append(char* out, DWORD capacity, DWORD* used, const char* value) {
    if (!out || !used || !value) return FALSE;
    for (DWORD i = 0; value[i]; ++i) {
        if (*used + 1 >= capacity) return FALSE;
        out[(*used)++] = value[i];
    }
    out[*used] = 0;
    return TRUE;
}

static BOOL AppendWideJson(char* out, DWORD capacity, DWORD* used, const WCHAR* value) {
    char* utf8 = g_utf8Scratch;
    if (!value || WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, utf8, sizeof(g_utf8Scratch), 0, 0) <= 0) return FALSE;
    if (!Append(out, capacity, used, "\"")) return FALSE;
    const char hex[] = "0123456789abcdef";
    for (DWORD i = 0; utf8[i]; ++i) {
        unsigned char c = (unsigned char)utf8[i];
        if (c == '"' || c == '\\') {
            if (*used + 2 >= capacity) return FALSE;
            out[(*used)++] = '\\'; out[(*used)++] = (char)c;
        } else if (c < 0x20) {
            if (*used + 6 >= capacity) return FALSE;
            out[(*used)++] = '\\'; out[(*used)++] = 'u'; out[(*used)++] = '0'; out[(*used)++] = '0';
            out[(*used)++] = hex[(c >> 4) & 0xf]; out[(*used)++] = hex[c & 0xf];
        } else {
            if (*used + 1 >= capacity) return FALSE;
            out[(*used)++] = (char)c;
        }
    }
    if (*used + 2 >= capacity) return FALSE;
    out[(*used)++] = '"'; out[*used] = 0;
    return TRUE;
}

static BOOL AppendUnsigned(char* out, DWORD capacity, DWORD* used, ULONGLONG value) {
    char digits[24];
    DWORD count = 0;
    do { digits[count++] = (char)('0' + (value % 10ULL)); value /= 10ULL; } while (value && count < 24);
    if (*used + count >= capacity) return FALSE;
    while (count) out[(*used)++] = digits[--count];
    out[*used] = 0;
    return TRUE;
}

static BOOL WriteText(LPCWSTR path, const char* text) {
    if (!path || !*path || !text) return FALSE;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, 0, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (!file || file == INVALID_HANDLE_VALUE) return FALSE;
    DWORD length = 0;
    while (text[length]) ++length;
    DWORD written = 0;
    BOOL ok = WriteFile(file, text, length, &written, 0) && written == length;
    CloseHandle(file);
    return ok;
}

static void WriteNativeReport(void) {
    if (g_boundState != 2 || !g_config.reportPath[0]) return;
    Lock(&g_reportLock);
    char json[1024];
    DWORD used = 0;
    json[0] = 0;
    Append(json, sizeof(json), &used, "{\"pid\":");
    AppendUnsigned(json, sizeof(json), &used, GetCurrentProcessId());
    Append(json, sizeof(json), &used, ",\"flags\":");
    AppendUnsigned(json, sizeof(json), &used, (DWORD)InterlockedCompareExchange(&g_flags, 0, 0));
    Append(json, sizeof(json), &used, ",\"errorCode\":");
    AppendUnsigned(json, sizeof(json), &used, (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0));
    LONG flags = InterlockedCompareExchange(&g_flags, 0, 0);
    Append(json, sizeof(json), &used, ",\"pending\":");
    Append(json, sizeof(json), &used, ((flags & FLAG_IAT_ARMED) && !(flags & FLAG_CALLBACK_CAPTURED) && !(flags & FLAG_CANCELLED)) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"callbackCaptured\":");
    Append(json, sizeof(json), &used, (flags & FLAG_CALLBACK_CAPTURED) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"capabilityScan\":");
    Append(json, sizeof(json), &used, (flags & FLAG_CAPABILITY_SCAN) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"iatArmed\":");
    Append(json, sizeof(json), &used, (flags & FLAG_IAT_ARMED) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"iatRestored\":");
    Append(json, sizeof(json), &used, (flags & FLAG_IAT_RESTORED) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"loaderAvailable\":");
    Append(json, sizeof(json), &used, (flags & FLAG_LOADER_AVAILABLE) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"bootstrapScheduled\":");
    Append(json, sizeof(json), &used, (flags & FLAG_BOOTSTRAP_SCHEDULED) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"scriptFailed\":");
    Append(json, sizeof(json), &used, (flags & FLAG_SCRIPT_FAILED) ? "true" : "false");
    Append(json, sizeof(json), &used, ",\"bootstrapOutcome\":\"pending-agent-report\",\"connectionGeneration\":");
    AppendUnsigned(json, sizeof(json), &used, g_config.connectionGeneration);
    Append(json, sizeof(json), &used, "}\n");
    WCHAR* shimReportPath = g_reportPathScratch;
    DWORD pathLength = 0;
    for (; g_config.reportPath[pathLength] && pathLength + 11 < 4200; ++pathLength) shimReportPath[pathLength] = g_config.reportPath[pathLength];
    if (g_config.reportPath[pathLength]) { Unlock(&g_reportLock); return; }
    const WCHAR suffix[] = L".shim.json";
    for (DWORD i = 0; suffix[i] && pathLength + 1 < 4200; ++i) shimReportPath[pathLength++] = suffix[i];
    shimReportPath[pathLength] = 0;
    WriteText(shimReportPath, json);
    Unlock(&g_reportLock);
}

static BOOL AppendConfigJson(char* out, DWORD capacity, DWORD* used, const ShimConfig* cfg) {
    if (!Append(out, capacity, used, "{\"modulePath\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->modulePath)) return FALSE;
    if (!Append(out, capacity, used, ",\"wrapperModulePath\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->wrapperModulePath)) return FALSE;
    if (!Append(out, capacity, used, ",\"pipeName\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->pipeName)) return FALSE;
    if (!Append(out, capacity, used, ",\"appPath\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->appPath)) return FALSE;
    if (!Append(out, capacity, used, ",\"sessionName\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->sessionName)) return FALSE;
    if (!Append(out, capacity, used, ",\"connectionGeneration\":")) return FALSE;
    if (!AppendUnsigned(out, capacity, used, cfg->connectionGeneration)) return FALSE;
    if (!Append(out, capacity, used, ",\"ipcToken\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->ipcToken)) return FALSE;
    if (!Append(out, capacity, used, ",\"reportPath\":")) return FALSE;
    if (!AppendWideJson(out, capacity, used, cfg->reportPath)) return FALSE;
    return Append(out, capacity, used, ",\"attach\":false}");
}

static BOOL BuildBootstrapScript(char* out, DWORD capacity, DWORD* length) {
    DWORD used = 0;
    out[0] = 0;
    if (!Append(out, capacity, &used, "(()=>{try{const c=")) return FALSE;
    if (!AppendConfigJson(out, capacity, &used, &g_config)) return FALSE;
    if (!Append(out, capacity, &used,
        ";globalThis.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__=c;const a=[];"
        "try{const r=globalThis.require;if(typeof r==='function')a.push({r,o:globalThis})}catch(e){}"
        "try{const p=typeof process==='undefined'?null:process;const m=p&&p.mainModule;const r=m&&m.require;if(typeof r==='function')a.push({r,o:m})}catch(e){}"
        "try{const p=typeof process==='undefined'?null:process;const g=p&&p.getBuiltinModule;const b=typeof g==='function'?g.call(p,'module'):null;const cr=b&&b.createRequire;if(typeof cr==='function'){const r=cr.call(b,c.modulePath);if(typeof r==='function')a.push({r,o:null})}}catch(e){}"
        "if(!a.length)return 1;if(typeof setImmediate!=='function')return 2;"
        "try{setImmediate(()=>{const write=(phase,code)=>{const d={phase,errorCode:code||null};"
        "try{const fs=a[0].r.call(a[0].o,'fs');fs.writeFileSync(c.reportPath+'.bootstrap.json',JSON.stringify(d),'utf8')}catch(e){}"
        "try{if(typeof process!=='undefined'&&process.stderr&&typeof process.stderr.write==='function')process.stderr.write('QQCALL_AGENT_'+phase+(code?'_'+code:'')+'\\n')}catch(e){}};"
        "let m=null;for(const l of a){try{m=l.r.call(l.o,c.modulePath);if(m)break}catch(e){}}"
        "if(!m){write('require_failed','MODULE_REQUIRE_FAILED');return}"
        "if(typeof m.bootstrap!=='function'){write('require_failed','BOOTSTRAP_EXPORT_MISSING');return}"
        "const safeCode=e=>{try{const d=Object.getOwnPropertyDescriptor(e,'code');const value=d&&Object.prototype.hasOwnProperty.call(d,'value')?d.value:null;"
        "return ['bootstrap_config_missing','bootstrap_module_path_invalid','app_path_invalid','invalid_pipe_name','generation_invalid','session_name_invalid','auth_token_invalid','wrapper_module_path_invalid','report_path_invalid','explicit_attach_required',"
        "'bootstrap_initializing','duplicate_bootstrap_active','pipe_disconnected','invalid_generation','invalid_auth_token','pipe_client_closed','pipe_connect_failed','pipe_not_connected','request_timeout','frame_too_large','backpressure_limit',"
        "'empty_frame','malformed_json','unterminated_frame','invalid_envelope','unsupported_schema','invalid_message_type','invalid_sequence','out_of_order_sequence','stale_generation','unauthorized','invalid_request_id',"
        "'event_request_id_must_be_null','request_id_required','operation_required','unexpected_response','response_operation_mismatch','request_failed','message_handler_failed','invalid_broker_frame','pipe_frame_error'].includes(value)?value:'BOOTSTRAP_REJECTED'}catch(_){return 'BOOTSTRAP_REJECTED'}};"
        "try{const p=m.bootstrap(c);if(p&&typeof p.then==='function')p.then(()=>write('bootstrap_returned',null),e=>write('bootstrap_failed',safeCode(e)));"
        "else write('bootstrap_returned',null)}catch(e){write('bootstrap_failed','BOOTSTRAP_THROW')}});return 3}"
        "catch(e){return 4}}catch(e){return 5}})()")) return FALSE;
    *length = used;
    return TRUE;
}

static BOOL RestoreOwnedSlot(void) {
    if (!g_iatSlot || !g_originalPointer) return FALSE;
    DWORD oldProtection = 0;
    if (!VirtualProtect(g_iatSlot, sizeof(void*), PAGE_READWRITE, &oldProtection)) { SetError(ERR_IAT_PROTECT); return FALSE; }
    void* replaced = InterlockedCompareExchangePointer((void* volatile*)g_iatSlot, g_originalPointer, (void*)&QQCallShimNapiGetCbInfo);
    DWORD ignored = 0;
    VirtualProtect(g_iatSlot, sizeof(void*), oldProtection, &ignored);
    if (replaced != (void*)&QQCallShimNapiGetCbInfo) { SetError(ERR_IAT_OWNERSHIP_LOST); return FALSE; }
    ClearFlag(FLAG_IAT_ARMED);
    SetFlag(FLAG_IAT_RESTORED);
    return TRUE;
}

static BOOL ClearShimException(napi_env env) {
    bool pending = false;
    if (!g_isExceptionPending || !g_clearException) { SetError(ERR_EXCEPTION_API); return FALSE; }
    napi_status status = g_isExceptionPending(env, &pending);
    if (status != 0) { SetError(ERR_EXCEPTION_API); return FALSE; }
    if (!pending) return TRUE;
    napi_value exceptionValue = 0;
    if (g_clearException(env, &exceptionValue) != 0) { SetError(ERR_CLEAR_EXCEPTION); return FALSE; }
    SetFlag(FLAG_JS_EXCEPTION_CLEARED);
    return TRUE;
}

static void ScheduleBootstrap(napi_env env) {
    bool pending = false;
    if (!g_isExceptionPending || !g_clearException || !g_getInt32 || !g_createString || !g_runScript) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_NAPI_EXPORT_MISSING); WriteNativeReport(); return;
    }
    napi_status status = g_isExceptionPending(env, &pending);
    if (status != 0) { SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_EXCEPTION_API); WriteNativeReport(); return; }
    if (pending) { SetError(ERR_PREEXISTING_EXCEPTION); WriteNativeReport(); return; }
    DWORD scriptLength = 0;
    if (!BuildBootstrapScript(g_script, sizeof(g_script), &scriptLength)) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_CONFIG_INVALID); WriteNativeReport(); return;
    }
    napi_value scriptValue = 0;
    status = g_createString(env, g_script, scriptLength, &scriptValue);
    if (status != 0) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_CREATE_STRING);
        ClearShimException(env);
        WriteNativeReport();
        return;
    }
    napi_value result = 0;
    status = g_runScript(env, scriptValue, &result);
    if (status != 0) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_RUN_SCRIPT);
        ClearShimException(env);
        WriteNativeReport();
        return;
    }
    int scriptStatus = 0;
    if (!result || g_getInt32(env, result, &scriptStatus) != 0) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_SCRIPT_STATUS);
        ClearShimException(env);
        WriteNativeReport();
        return;
    }
    if (scriptStatus == 1) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_LOADER_UNAVAILABLE);
    } else if (scriptStatus == 2) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_SETIMMEDIATE_UNAVAILABLE);
        SetFlag(FLAG_LOADER_AVAILABLE);
    } else if (scriptStatus == 3) {
        SetFlag(FLAG_LOADER_AVAILABLE);
        SetFlag(FLAG_BOOTSTRAP_SCHEDULED);
        SetFlag(FLAG_AGENT_OUTCOME_PENDING);
        SetError(ERR_NONE);
    } else if (scriptStatus == 4) {
        SetFlag(FLAG_LOADER_AVAILABLE);
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_RUN_SCRIPT);
    } else if (scriptStatus == 5) {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_RUN_SCRIPT);
    } else {
        SetFlag(FLAG_SCRIPT_FAILED); SetError(ERR_SCRIPT_STATUS);
    }
    WriteNativeReport();
}

static DWORD WINAPI QQCallShimNapiGetCbInfo(napi_env env, napi_callback_info info, SIZE_T* argc, napi_value* argv, napi_value* thisArg, void** data) {
    napi_get_cb_info_fn original = g_original;
    if (!original) return 1;
    napi_status result = original(env, info, argc, argv, thisArg, data);
    if (result != 0) return (DWORD)result;
    Lock(&g_transitionLock);
    if (g_cancelled || InterlockedCompareExchange(&g_callbackClaimed, 0, 0) != 0) {
        Unlock(&g_transitionLock);
        return (DWORD)result;
    }
    InterlockedExchange(&g_callbackClaimed, 1);
    SetFlag(FLAG_CALLBACK_CAPTURED);
    SetError(ERR_NONE);
    BOOL restored = RestoreOwnedSlot();
    Unlock(&g_transitionLock);
    if (restored) ScheduleBootstrap(env);
    else WriteNativeReport();
    return (DWORD)result;
}

static BOOL ScanAndArm(void) {
    WCHAR* wrapperPath = g_wrapperPathScratch;
    wrapperPath[0] = 0;
    BYTE* wrapper = 0;
    BYTE* qqnt = 0;
    DWORD wrapperSize = 0, qqntSize = 0;
    if (!FindModules(&wrapper, &wrapperSize, wrapperPath, 4096, &qqnt, &qqntSize)) {
        if (InterlockedCompareExchange(&g_errorCode, 0, 0) == ERR_NONE) SetError(ERR_WRAPPER_NOT_FOUND);
        return FALSE;
    }
    DWORD pathLength = 0;
    for (; wrapperPath[pathLength]; ++pathLength) {
        if (pathLength + 1 >= 4096) { SetError(ERR_WRAPPER_AMBIGUOUS); return FALSE; }
    }
    DWORD slotRva = 0;
    if (!ImageImportSlot(wrapper, wrapperSize, &slotRva)) { SetError(ERR_IMPORT_LAYOUT); return FALSE; }
    FARPROC originalExport = GetProcAddress((HMODULE)qqnt, "napi_get_cb_info");
    napi_create_string_utf8_fn createString = (napi_create_string_utf8_fn)GetProcAddress((HMODULE)qqnt, "napi_create_string_utf8");
    napi_run_script_fn runScript = (napi_run_script_fn)GetProcAddress((HMODULE)qqnt, "napi_run_script");
    napi_is_exception_pending_fn isExceptionPending = (napi_is_exception_pending_fn)GetProcAddress((HMODULE)qqnt, "napi_is_exception_pending");
    napi_get_and_clear_last_exception_fn clearException = (napi_get_and_clear_last_exception_fn)GetProcAddress((HMODULE)qqnt, "napi_get_and_clear_last_exception");
    napi_get_value_int32_fn getInt32 = (napi_get_value_int32_fn)GetProcAddress((HMODULE)qqnt, "napi_get_value_int32");
    if (!originalExport || !createString || !runScript || !isExceptionPending || !clearException || !getInt32) {
        SetError(ERR_NAPI_EXPORT_MISSING); return FALSE;
    }
    SetFlag(FLAG_CAPABILITY_SCAN);
    void** candidateSlot = (void**)(wrapper + slotRva);
    void* candidateOriginal = *candidateSlot;
    if (candidateOriginal != (void*)originalExport) { SetError(ERR_IMPORT_ALREADY_HOOKED); return FALSE; }
    Lock(&g_transitionLock);
    if (g_cancelled) {
        SetError(ERR_CANCELLED); Unlock(&g_transitionLock); return FALSE;
    }
    DWORD oldProtection = 0;
    if (!VirtualProtect(candidateSlot, sizeof(void*), PAGE_READWRITE, &oldProtection)) {
        Unlock(&g_transitionLock); SetError(ERR_IAT_PROTECT); return FALSE;
    }
    // Publish only the validated vendor target. The callback may run as soon as
    // the CAS succeeds, so these values must exist before exposing our pointer.
    g_wrapperBase = wrapper; g_wrapperSize = wrapperSize;
    g_qqntBase = qqnt; g_qqntSize = qqntSize;
    g_iatSlot = candidateSlot;
    g_originalPointer = candidateOriginal;
    g_original = (napi_get_cb_info_fn)candidateOriginal;
    g_createString = createString;
    g_runScript = runScript;
    g_isExceptionPending = isExceptionPending;
    g_clearException = clearException;
    g_getInt32 = getInt32;
    void* replaced = InterlockedCompareExchangePointer((void* volatile*)candidateSlot, (void*)&QQCallShimNapiGetCbInfo, candidateOriginal);
    DWORD ignored = 0;
    VirtualProtect(candidateSlot, sizeof(void*), oldProtection, &ignored);
    if (replaced != candidateOriginal) {
        g_iatSlot = 0;
        g_originalPointer = 0;
        g_original = 0;
        g_createString = 0;
        g_runScript = 0;
        g_isExceptionPending = 0;
        g_clearException = 0;
        g_getInt32 = 0;
        SetError(ERR_IAT_CAS);
        Unlock(&g_transitionLock);
        return FALSE;
    }
    CopyWide(g_config.wrapperModulePath, wrapperPath, 4096);
    SetFlag(FLAG_IAT_ARMED);
    SetError(ERR_NONE);
    Unlock(&g_transitionLock);
    return TRUE;
}

extern "C" DWORD WINAPI QQCallShimStart(const WCHAR* configWidePathPointer) {
    if (!BindConfig(configWidePathPointer)) { WriteNativeReport(); return (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0); }
    DWORD terminalResult = ERR_NONE;
    BOOL alreadyHandled = FALSE;
    if (InterlockedCompareExchange(&g_cancelled, 0, 0)) {
        terminalResult = ERR_CANCELLED;
        alreadyHandled = TRUE;
    } else if (InterlockedCompareExchange(&g_callbackClaimed, 0, 0) ||
               (InterlockedCompareExchange(&g_flags, 0, 0) & (FLAG_IAT_ARMED | FLAG_BOOTSTRAP_SCHEDULED))) {
        alreadyHandled = TRUE;
    }
    if (alreadyHandled) {
        if (terminalResult != ERR_NONE) SetError(terminalResult);
        WriteNativeReport(); return terminalResult;
    }
#if defined(QQCALL_SHIM_TEST_HOOKS)
    TestPauseAfterStartFastPath();
#endif
    if (InterlockedCompareExchange(&g_startLock, 1, 0) != 0) { SetError(ERR_CONTROLLER_BUSY); return ERR_CONTROLLER_BUSY; }
    Lock(&g_transitionLock);
    if (InterlockedCompareExchange(&g_cancelled, 0, 0)) {
        terminalResult = ERR_CANCELLED;
        Unlock(&g_transitionLock);
        SetError(terminalResult);
        InterlockedExchange(&g_startLock, 0);
        WriteNativeReport();
        return terminalResult;
    }
    if (InterlockedCompareExchange(&g_callbackClaimed, 0, 0) ||
        (InterlockedCompareExchange(&g_flags, 0, 0) & (FLAG_IAT_ARMED | FLAG_BOOTSTRAP_SCHEDULED))) {
        Unlock(&g_transitionLock);
        InterlockedExchange(&g_startLock, 0);
        WriteNativeReport();
        return ERR_NONE;
    }
    Unlock(&g_transitionLock);
    BOOL ok = ScanAndArm();
    InterlockedExchange(&g_startLock, 0);
    WriteNativeReport();
    return ok ? ERR_NONE : (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0);
}

#if defined(QQCALL_SHIM_TEST_HOOKS)
extern "C" void WINAPI QQCallShimTestPauseStartCall(DWORD ordinal) {
    InterlockedExchange(&g_testStartOrdinal, 0);
    InterlockedExchange(&g_testPauseOrdinal, (LONG)ordinal);
    InterlockedExchange(&g_testStartPaused, 0);
    InterlockedExchange(&g_testStartReleased, 0);
}

extern "C" DWORD WINAPI QQCallShimTestWaitStartPaused(DWORD timeoutMs) {
    for (DWORD elapsed = 0; elapsed < timeoutMs; ++elapsed) {
        if (InterlockedCompareExchange(&g_testStartPaused, 0, 0)) return 1;
        Sleep(1);
    }
    return InterlockedCompareExchange(&g_testStartPaused, 0, 0) ? 1 : 0;
}

extern "C" void WINAPI QQCallShimTestReleaseStartCall(void) {
    InterlockedExchange(&g_testStartReleased, 1);
}
#endif

extern "C" DWORD WINAPI QQCallShimCancel(const WCHAR* configWidePathPointer) {
    if (!BindConfig(configWidePathPointer)) { WriteNativeReport(); return (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0); }
    Lock(&g_transitionLock);
    if (InterlockedCompareExchange(&g_callbackClaimed, 0, 0) != 0) {
        Unlock(&g_transitionLock); SetError(ERR_CANCEL_TOO_LATE); WriteNativeReport(); return ERR_CANCEL_TOO_LATE;
    }
    if (InterlockedCompareExchange(&g_cancelled, 0, 0) != 0) {
        Unlock(&g_transitionLock); SetError(ERR_NONE); WriteNativeReport(); return ERR_NONE;
    }
    if ((InterlockedCompareExchange(&g_flags, 0, 0) & FLAG_IAT_ARMED) && !RestoreOwnedSlot()) {
        Unlock(&g_transitionLock);
        WriteNativeReport();
        return (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0);
    }
    InterlockedExchange(&g_cancelled, 1);
    SetFlag(FLAG_CANCELLED);
    SetError(ERR_NONE);
    Unlock(&g_transitionLock);
    WriteNativeReport();
    return ERR_NONE;
}

extern "C" DWORD WINAPI QQCallShimGetStatus(QQCallShimStatusSnapshot* output) {
    if (!output) return ERR_STATUS_POINTER;
    QQCallShimStatusSnapshot snapshot;
    snapshot.size = sizeof(QQCallShimStatusSnapshot);
    snapshot.version = 1;
    snapshot.flags = (DWORD)InterlockedCompareExchange(&g_flags, 0, 0);
    snapshot.errorCode = (DWORD)InterlockedCompareExchange(&g_errorCode, 0, 0);
    snapshot.processId = GetCurrentProcessId();
    snapshot.reserved = 0;
    snapshot.connectionGeneration = g_boundState == 2 ? g_config.connectionGeneration : 0;
    *output = snapshot;
    WriteNativeReport();
    return ERR_NONE;
}

extern "C" BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
