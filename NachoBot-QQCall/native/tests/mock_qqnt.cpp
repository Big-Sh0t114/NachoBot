typedef unsigned long DWORD;
typedef unsigned long long SIZE_T;
typedef int napi_status;
typedef void* napi_env;
typedef void* napi_value;
typedef void* napi_callback_info;
typedef int BOOL;
typedef void* HINSTANCE;
typedef void* LPVOID;
#define WINAPI __stdcall
#define TRUE 1
#define FALSE 0
#define DLL_PROCESS_ATTACH 1

struct FakeEnv {
    int originalStatus;
    int exceptionPending;
    int createStringStatus;
    int runScriptStatus;
    int scriptStatus;
    int originalCalls;
    int createStringCalls;
    int runScriptCalls;
    int clearCalls;
    char script[32768];
};

static FakeEnv g_env;

extern "C" napi_status napi_get_cb_info(napi_env env, napi_callback_info, SIZE_T*, napi_value*, napi_value*, void**) {
    FakeEnv* state = (FakeEnv*)env;
    if (!state) state = &g_env;
    ++state->originalCalls;
    return state->originalStatus;
}

extern "C" napi_status napi_create_string_utf8(napi_env env, const char* text, SIZE_T length, napi_value* output) {
    FakeEnv* state = (FakeEnv*)env;
    if (!state) state = &g_env;
    ++state->createStringCalls;
    if (state->createStringStatus != 0) return state->createStringStatus;
    if (length >= sizeof(state->script)) return 90;
    for (SIZE_T i = 0; i < length; ++i) state->script[i] = text[i];
    state->script[length] = 0;
    *output = (napi_value)1;
    return 0;
}

extern "C" napi_status napi_run_script(napi_env env, napi_value script, napi_value* result) {
    FakeEnv* state = (FakeEnv*)env;
    if (!state) state = &g_env;
    ++state->runScriptCalls;
    if (script != (napi_value)1) return 91;
    if (state->runScriptStatus != 0) {
        state->exceptionPending = 1;
        return state->runScriptStatus;
    }
    *result = (napi_value)(long long)(state->scriptStatus + 1);
    return 0;
}

extern "C" napi_status napi_is_exception_pending(napi_env env, bool* pending) {
    FakeEnv* state = (FakeEnv*)env;
    if (!state) state = &g_env;
    *pending = state->exceptionPending != 0;
    return 0;
}

extern "C" napi_status napi_get_and_clear_last_exception(napi_env env, napi_value* exception) {
    FakeEnv* state = (FakeEnv*)env;
    if (!state) state = &g_env;
    ++state->clearCalls;
    *exception = (napi_value)1;
    state->exceptionPending = 0;
    return 0;
}

extern "C" napi_status napi_get_value_int32(napi_env env, napi_value value, int* output) {
    (void)env;
    if (!value || !output) return 92;
    *output = (int)(long long)value - 1;
    return 0;
}

extern "C" void WINAPI MockConfigure(int originalStatus, int exceptionPending, int createStringStatus, int runScriptStatus, int scriptStatus) {
    g_env.originalStatus = originalStatus;
    g_env.exceptionPending = exceptionPending;
    g_env.createStringStatus = createStringStatus;
    g_env.runScriptStatus = runScriptStatus;
    g_env.scriptStatus = scriptStatus;
    g_env.originalCalls = 0;
    g_env.createStringCalls = 0;
    g_env.runScriptCalls = 0;
    g_env.clearCalls = 0;
    g_env.script[0] = 0;
}

extern "C" int WINAPI MockGetOriginalCalls(void) { return g_env.originalCalls; }
extern "C" int WINAPI MockGetRunScriptCalls(void) { return g_env.runScriptCalls; }
extern "C" int WINAPI MockGetClearCalls(void) { return g_env.clearCalls; }
extern "C" int WINAPI MockGetPending(void) { return g_env.exceptionPending; }
extern "C" void* WINAPI MockGetEnv(void) { return &g_env; }
extern "C" DWORD WINAPI MockGetScriptLength(void) {
    DWORD length = 0;
    while (g_env.script[length]) ++length;
    return length;
}
extern "C" DWORD WINAPI MockCopyScript(char* output, DWORD capacity) {
    DWORD length = MockGetScriptLength();
    if (!output || capacity <= length) return 0;
    for (DWORD i = 0; i <= length; ++i) output[i] = g_env.script[i];
    return length;
}

extern "C" int WINAPI DllMain(HINSTANCE, DWORD, LPVOID) { return TRUE; }
