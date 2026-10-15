typedef unsigned char BYTE;
typedef unsigned short WORD;
typedef unsigned long DWORD;
typedef unsigned long long SIZE_T;
typedef int BOOL;
typedef void* HANDLE;
typedef void* HMODULE;
typedef void* FARPROC;
typedef void* napi_env;
typedef void* napi_value;
typedef void* napi_callback_info;
typedef wchar_t WCHAR;
#define WINAPI __stdcall
#define TRUE 1
#define FALSE 0
#define STD_INPUT_HANDLE ((DWORD)-10)
#define STD_OUTPUT_HANDLE ((DWORD)-11)

extern "C" __declspec(dllimport) HMODULE WINAPI LoadLibraryW(const WCHAR*);
extern "C" __declspec(dllimport) FARPROC WINAPI GetProcAddress(HMODULE, const char*);
extern "C" __declspec(dllimport) DWORD WINAPI GetCurrentProcessId(void);
extern "C" __declspec(dllimport) BOOL WINAPI CloseHandle(HANDLE);
extern "C" __declspec(dllimport) HANDLE WINAPI GetStdHandle(DWORD);
extern "C" __declspec(dllimport) BOOL WINAPI ReadFile(HANDLE, void*, DWORD, DWORD*, void*);
extern "C" __declspec(dllimport) BOOL WINAPI WriteFile(HANDLE, const void*, DWORD, DWORD*, void*);
extern "C" __declspec(dllimport) void WINAPI ExitProcess(DWORD);
extern "C" __declspec(dllimport) WCHAR* WINAPI GetCommandLineW(void);
extern "C" __declspec(dllimport) DWORD WINAPI GetLastError(void);
typedef DWORD (WINAPI *ThreadStartFn)(void*);
extern "C" __declspec(dllimport) HANDLE WINAPI CreateThread(void*, SIZE_T, ThreadStartFn, void*, DWORD, DWORD*);
extern "C" __declspec(dllimport) DWORD WINAPI WaitForSingleObject(HANDLE, DWORD);

struct ShimStatus {
    DWORD size;
    DWORD version;
    DWORD flags;
    DWORD errorCode;
    DWORD processId;
    DWORD reserved;
    unsigned long long connectionGeneration;
};

typedef void (WINAPI *MockConfigureFn)(int, int, int, int, int);
typedef int (WINAPI *MockIntFn)(void);
typedef void* (WINAPI *MockGetEnvFn)(void);
typedef DWORD (WINAPI *MockCopyScriptFn)(char*, DWORD);
typedef int (WINAPI *WrapperInvokeFn)(napi_env);
typedef int (WINAPI *WrapperInstallForeignFn)(void);
typedef void* (WINAPI *WrapperGetImportFn)(void);
typedef DWORD (WINAPI *ShimStartFn)(const WCHAR*);
typedef DWORD (WINAPI *ShimCancelFn)(const WCHAR*);
typedef DWORD (WINAPI *ShimStatusFn)(ShimStatus*);
typedef void (WINAPI *ShimTestPauseFn)(DWORD);
typedef DWORD (WINAPI *ShimTestWaitPausedFn)(DWORD);
typedef void (WINAPI *ShimTestReleaseFn)(void);

struct StartInvocation {
    ShimStartFn start;
    const WCHAR* configPath;
    DWORD result;
};

static DWORD WINAPI StartInvocationProc(void* context) {
    StartInvocation* invocation = (StartInvocation*)context;
    invocation->result = invocation->start(invocation->configPath);
    return invocation->result;
}

static WCHAR g_scenario[64];
static WCHAR g_configPath[4096];
static char g_output[40000];

static void WriteOut(const char* text, DWORD length) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output) WriteFile(output, text, length, &written, 0);
}

static void WriteText(const char* text) {
    DWORD length = 0;
    while (text[length]) ++length;
    WriteOut(text, length);
}

static DWORD CopyArg(WCHAR* input, WCHAR* output, DWORD capacity) {
    while (*input == L' ' || *input == L'\t') ++input;
    BOOL quoted = *input == L'"';
    if (quoted) ++input;
    DWORD i = 0;
    while (*input && (quoted ? *input != L'"' : (*input != L' ' && *input != L'\t'))) {
        if (i + 1 >= capacity) return 0;
        output[i++] = *input++;
    }
    output[i] = 0;
    return i;
}

static WCHAR* SkipArg(WCHAR* input) {
    while (*input == L' ' || *input == L'\t') ++input;
    BOOL quoted = *input == L'"';
    if (quoted) ++input;
    while (*input && (quoted ? *input != L'"' : (*input != L' ' && *input != L'\t'))) ++input;
    if (quoted && *input == L'"') ++input;
    while (*input == L' ' || *input == L'\t') ++input;
    return input;
}

static BOOL IsScenario(const WCHAR* expected) {
    DWORD i = 0;
    while (g_scenario[i] && expected[i] && g_scenario[i] == expected[i]) ++i;
    return !g_scenario[i] && !expected[i];
}

static void Fail(DWORD code) {
    WriteText("FAIL ");
    char digits[16]; DWORD count = 0;
    do { digits[count++] = (char)('0' + code % 10); code /= 10; } while (code);
    while (count) { char c = digits[--count]; WriteOut(&c, 1); }
    WriteText("\n");
    ExitProcess(1);
}

static void Require(BOOL condition, DWORD code) { if (!condition) Fail(code); }

static BOOL Contains(const char* text, const char* pattern) {
    for (DWORD i = 0; text[i]; ++i) {
        DWORD j = 0;
        while (pattern[j] && text[i + j] == pattern[j]) ++j;
        if (!pattern[j]) return TRUE;
    }
    return FALSE;
}

extern "C" void WINAPI HarnessMain(void) {
    WCHAR* command = GetCommandLineW();
    command = SkipArg(command);
    if (!CopyArg(command, g_scenario, 64)) { WriteText("FAIL bad-scenario\n"); ExitProcess(2); }
    command = SkipArg(command);
    if (!CopyArg(command, g_configPath, 4096)) { WriteText("FAIL bad-config-path\n"); ExitProcess(2); }

    char ready[64];
    ready[0] = 'R'; ready[1] = 'E'; ready[2] = 'A'; ready[3] = 'D'; ready[4] = 'Y'; ready[5] = ' ';
    DWORD pid = GetCurrentProcessId();
    char digits[16]; DWORD count = 0;
    do { digits[count++] = (char)('0' + pid % 10); pid /= 10; } while (pid);
    DWORD used = 6;
    while (count) ready[used++] = digits[--count];
    ready[used++] = '\n';
    WriteOut(ready, used);
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    char signal = 0; DWORD read = 0;
    if (!ReadFile(input, &signal, 1, &read, 0) || read != 1) { WriteText("FAIL config-handshake\n"); ExitProcess(2); }

    HMODULE qqnt = LoadLibraryW(L"QQNT.dll");
    HMODULE wrapper = LoadLibraryW(L"wrapper.node");
    HMODULE shim = LoadLibraryW(L"QQCallShim.dll");
    Require(qqnt != 0, 31);
    if (!wrapper) Fail(3200 + GetLastError());
    if (!shim) Fail(3300 + GetLastError());
    MockConfigureFn configure = (MockConfigureFn)GetProcAddress(qqnt, "MockConfigure");
    MockIntFn originalCalls = (MockIntFn)GetProcAddress(qqnt, "MockGetOriginalCalls");
    MockIntFn runCalls = (MockIntFn)GetProcAddress(qqnt, "MockGetRunScriptCalls");
    MockIntFn clearCalls = (MockIntFn)GetProcAddress(qqnt, "MockGetClearCalls");
    MockIntFn pending = (MockIntFn)GetProcAddress(qqnt, "MockGetPending");
    MockGetEnvFn getEnv = (MockGetEnvFn)GetProcAddress(qqnt, "MockGetEnv");
    MockCopyScriptFn copyScript = (MockCopyScriptFn)GetProcAddress(qqnt, "MockCopyScript");
    WrapperInvokeFn invoke = (WrapperInvokeFn)GetProcAddress(wrapper, "WrapperInvoke");
    WrapperInstallForeignFn installForeign = (WrapperInstallForeignFn)GetProcAddress(wrapper, "WrapperInstallForeign");
    WrapperGetImportFn getImport = (WrapperGetImportFn)GetProcAddress(wrapper, "WrapperGetCbInfoImport");
    ShimStartFn start = (ShimStartFn)GetProcAddress(shim, "QQCallShimStart");
    ShimCancelFn cancel = (ShimCancelFn)GetProcAddress(shim, "QQCallShimCancel");
    ShimStatusFn getStatus = (ShimStatusFn)GetProcAddress(shim, "QQCallShimGetStatus");
    ShimTestPauseFn testPause = (ShimTestPauseFn)GetProcAddress(shim, "QQCallShimTestPauseStartCall");
    ShimTestWaitPausedFn testWaitPaused = (ShimTestWaitPausedFn)GetProcAddress(shim, "QQCallShimTestWaitStartPaused");
    ShimTestReleaseFn testRelease = (ShimTestReleaseFn)GetProcAddress(shim, "QQCallShimTestReleaseStartCall");
    FARPROC originalGetCbInfo = GetProcAddress(qqnt, "napi_get_cb_info");
    Require(configure && originalCalls && runCalls && clearCalls && pending && getEnv && copyScript && invoke && installForeign && getImport && start && cancel && getStatus && testPause && testWaitPaused && testRelease && originalGetCbInfo, 4);
    napi_env env = getEnv();
    ShimStatus status;
    int invokeResult = 0;
    int setupForeign = IsScenario(L"foreign-hook") ? installForeign() : 0;
    if (IsScenario(L"foreign-hook")) Require(setupForeign == 0, 5);

    if (IsScenario(L"preexisting-exception")) configure(0, 1, 0, 0, 3);
    else if (IsScenario(L"shim-exception")) configure(0, 0, 0, 23, 3);
    else if (IsScenario(L"original-fails")) configure(17, 0, 0, 0, 3);
    else if (IsScenario(L"loader-unavailable")) configure(0, 0, 0, 0, 1);
    else if (IsScenario(L"setimmediate-unavailable")) configure(0, 0, 0, 0, 2);
    else if (IsScenario(L"schedule-throws")) configure(0, 0, 0, 0, 4);
    else configure(0, 0, 0, 0, 3);

    if (IsScenario(L"overlapping-starts")) {
        testPause(1);
        StartInvocation invocation = { start, g_configPath, 0xffffffffUL };
        HANDLE thread = CreateThread(0, 0, StartInvocationProc, &invocation, 0, 0);
        Require(thread != 0, 36);
        Require(testWaitPaused(5000) == 1, 37);
        DWORD firstResult = start(g_configPath);
        Require(firstResult == 0 && getImport() != originalGetCbInfo, 38);
        testRelease();
        Require(WaitForSingleObject(thread, 5000) == 0, 39);
        CloseHandle(thread);
        Require(invocation.result == 0 && getImport() != originalGetCbInfo, 40);
        Require(getStatus(&status) == 0 && (status.flags & 2) && !(status.flags & 0x100), 41);
        Require(cancel(g_configPath) == 0, 42);
        Require(getStatus(&status) == 0 && (status.flags & 8) && (status.flags & 16) && !(status.flags & 0x100), 43);
        Require(getImport() == originalGetCbInfo && runCalls() == 0, 44);
    } else {
        DWORD startResult = start(g_configPath);
        if (IsScenario(L"foreign-hook")) {
        Require(startResult == 25, 10);
        invokeResult = invoke(env);
        Require(invokeResult == 77 && runCalls() == 0, 11);
        Require(getStatus(&status) == 0 && !(status.flags & 2), 12);
        } else {
        Require(startResult == 0, 13);
        Require(getStatus(&status) == 0 && (status.flags & 2), 14);
        if (IsScenario(L"cancel")) {
            Require(cancel(g_configPath) == 0, 15);
            Require(getStatus(&status) == 0 && (status.flags & 8) && (status.flags & 16) && !(status.flags & 0x100), 16);
            Require(getImport() == originalGetCbInfo, 45);
            invokeResult = invoke(env);
            Require(invokeResult == 0 && runCalls() == 0 && originalCalls() == 1, 17);
            Require(cancel(g_configPath) == 0, 18);
        } else if (IsScenario(L"original-fails")) {
            invokeResult = invoke(env);
            Require(invokeResult == 17 && runCalls() == 0, 19);
            Require(getStatus(&status) == 0 && (status.flags & 2) && !(status.flags & 0x100), 20);
            configure(0, 0, 0, 0, 3);
            invokeResult = invoke(env);
            Require(invokeResult == 0 && runCalls() == 1, 21);
            Require(getImport() == originalGetCbInfo, 46);
            Require(getStatus(&status) == 0 && (status.flags & 0x100) && (status.flags & 8) && (status.flags & 4), 22);
            Require(cancel(g_configPath) == 30, 23);
        } else {
            invokeResult = invoke(env);
            Require(invokeResult == 0, 24);
            Require(getImport() == originalGetCbInfo, 47);
            Require(getStatus(&status) == 0, 25);
            Require((status.flags & 0x100) && (status.flags & 8) && !(status.flags & 2), 26);
            if (IsScenario(L"preexisting-exception")) {
                Require(runCalls() == 0 && clearCalls() == 0 && pending() == 1 && status.errorCode == 41, 27);
            } else if (IsScenario(L"shim-exception")) {
                Require(runCalls() == 1 && clearCalls() == 1 && pending() == 0 && (status.flags & 0x80) && (status.flags & 0x200), 28);
            } else if (IsScenario(L"loader-unavailable")) {
                Require(runCalls() == 1 && !(status.flags & 0x20) && (status.flags & 0x200) && status.errorCode == 46, 29);
            } else if (IsScenario(L"setimmediate-unavailable")) {
                Require(runCalls() == 1 && (status.flags & 0x20) && !(status.flags & 4) && status.errorCode == 47, 30);
            } else if (IsScenario(L"schedule-throws")) {
                Require(runCalls() == 1 && (status.flags & 0x20) && !(status.flags & 4) && status.errorCode == 43, 31);
            } else {
                Require(runCalls() == 1 && (status.flags & 0x20) && (status.flags & 4) && (status.flags & 0x40), 32);
                invokeResult = invoke(env);
                Require(invokeResult == 0 && runCalls() == 1, 33);
                DWORD length = copyScript(g_output, sizeof(g_output));
                Require(length > 0 && Contains(g_output, "__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__") && Contains(g_output, "setImmediate"), 34);
                WriteText("SCRIPT_BEGIN\n"); WriteOut(g_output, length); WriteText("\nSCRIPT_END\n");
                Require(cancel(g_configPath) == 30, 35);
            }
        }
        }
    }

    char line[128];
    line[0] = 'P'; line[1] = 'A'; line[2] = 'S'; line[3] = 'S'; line[4] = ' ';
    line[5] = 'f'; line[6] = 'l'; line[7] = 'a'; line[8] = 'g'; line[9] = 's'; line[10] = '=';
    DWORD lineUsed = 11;
    DWORD values[4] = { status.flags, status.errorCode, (DWORD)originalCalls(), (DWORD)runCalls() };
    for (DWORD v = 0; v < 4; ++v) {
        DWORD n = values[v]; char reversed[16]; DWORD digitsCount = 0;
        do { reversed[digitsCount++] = (char)('0' + n % 10); n /= 10; } while (n);
        while (digitsCount) line[lineUsed++] = reversed[--digitsCount];
        line[lineUsed++] = v == 3 ? '\n' : ',';
    }
    WriteOut(line, lineUsed);
    ExitProcess(0);
}
