#include "node_api_compat.h"
#include "parent_hooks.h"

#include <tlhelp32.h>

#include <string>
#include <vector>

namespace qar {
namespace {

NapiApi g_napi = {};
bool g_napiReady = false;

FARPROC FindNodeApi(const char* name) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) return nullptr;
    MODULEENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    FARPROC result = nullptr;
    if (Module32FirstW(snapshot, &entry)) {
        do {
            HMODULE module = GetModuleHandleW(entry.szModule);
            if (module && (result = GetProcAddress(module, name)) != nullptr) break;
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return result;
}

bool ReadString(napi_env env, napi_value object, const char* name, std::wstring* output,
                size_t maximumCharacters, bool required) {
    if (!output) return false;
    napi_value property = nullptr;
    if (g_napi.get_named_property(env, object, name, &property) != 0 || !property) return !required;
    size_t length = 0;
    if (g_napi.get_value_string_utf8(env, property, nullptr, 0, &length) != 0 ||
        length == 0 || length > maximumCharacters * 4) return false;
    std::vector<char> utf8(length + 1);
    size_t actual = 0;
    if (g_napi.get_value_string_utf8(env, property, utf8.data(), utf8.size(), &actual) != 0 ||
        actual != length || utf8[length] != '\0') return false;
    const int wideLength = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8.data(),
                                               static_cast<int>(length), nullptr, 0);
    if (wideLength <= 0 || static_cast<size_t>(wideLength) > maximumCharacters) return false;
    std::vector<wchar_t> wide(static_cast<size_t>(wideLength) + 1);
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8.data(), static_cast<int>(length),
                            wide.data(), wideLength) != wideLength) return false;
    wide[wideLength] = L'\0';
    output->assign(wide.data(), wideLength);
    return output->find(L'\0') == std::wstring::npos;
}

napi_value NewBool(napi_env env, bool value) {
    napi_value result = nullptr;
    if (g_napi.get_boolean(env, value, &result) != 0) return nullptr;
    return result;
}

napi_value NewUint(napi_env env, uint32_t value) {
    napi_value result = nullptr;
    if (g_napi.create_uint32(env, value, &result) != 0) return nullptr;
    return result;
}

napi_value NewDouble(napi_env env, double value) {
    napi_value result = nullptr;
    if (g_napi.create_double(env, value, &result) != 0) return nullptr;
    return result;
}

void Set(napi_env env, napi_value object, const char* key, napi_value value) {
    if (value) g_napi.set_named_property(env, object, key, value);
}

napi_value MakeResult(napi_env env, bool installed, FailureCode failure, uint64_t generation) {
    napi_value object = nullptr;
    if (g_napi.create_object(env, &object) != 0 || !object) return nullptr;
    Set(env, object, "installed", NewBool(env, installed));
    Set(env, object, "failureCode", NewUint(env, static_cast<uint32_t>(failure)));
    Set(env, object, "generation", NewDouble(env, static_cast<double>(generation)));
    return object;
}

napi_value InstallCallback(napi_env env, napi_callback_info info) {
    if (!g_napiReady) return nullptr;
    size_t argc = 1;
    napi_value argv[1] = {};
    if (g_napi.get_cb_info(env, info, &argc, argv, nullptr, nullptr) != 0 || argc != 1 || !argv[0])
        return MakeResult(env, false, FailureCode::config_invalid, 0);
    ParentOptions options;
    if (!ReadString(env, argv[0], "qqImagePath", &options.qqImagePath, kMaxImagePathChars - 1, true) ||
        !ReadString(env, argv[0], "guardDllPath", &options.guardDllPath, kMaxImagePathChars - 1, true) ||
        !ReadString(env, argv[0], "qqBuild", &options.qqBuild, kMaxBuildChars - 1, true) ||
        !ReadString(env, argv[0], "captureEndpointId", &options.captureEndpointId,
                    kMaxEndpointIdChars - 1, true) ||
        !ReadString(env, argv[0], "renderEndpointId", &options.renderEndpointId,
                    kMaxEndpointIdChars - 1, true)) return MakeResult(env, false, FailureCode::config_invalid, 0);
    double generation = 0;
    napi_value generationValue = nullptr;
    napi_valuetype generationType = 0;
    if (g_napi.get_named_property(env, argv[0], "hostGeneration", &generationValue) != 0 ||
        !generationValue || g_napi.typeof_value(env, generationValue, &generationType) != 0 ||
        generationType != kNapiNumberType || g_napi.get_value_double(env, generationValue, &generation) != 0 ||
        !ParseSafeGeneration(true, generation, &options.hostGeneration))
        return MakeResult(env, false, FailureCode::config_invalid, 0);
    FailureCode failure = FailureCode::none;
    const bool installed = InstallParentLaunchGuard(options, &failure);
    return MakeResult(env, installed, failure, options.hostGeneration);
}

napi_value StatusCallback(napi_env env, napi_callback_info info) {
    (void)info;
    if (!g_napiReady) return nullptr;
    return MakeResult(env, ParentLaunchGuardInstalled(),
                      ParentLaunchGuardInstalled() ? FailureCode::none : FailureCode::config_invalid,
                      ParentGuardGeneration());
}

} // namespace

bool InitializeNapiApi(NapiApi* api) {
    if (!api) return false;
    NapiApi candidate = {};
#define QAR_NAPI_LOAD(field, symbol) candidate.field = reinterpret_cast<decltype(candidate.field)>(FindNodeApi(symbol));
    QAR_NAPI_LOAD(get_cb_info, "napi_get_cb_info")
    QAR_NAPI_LOAD(get_named_property, "napi_get_named_property")
    QAR_NAPI_LOAD(typeof_value, "napi_typeof")
    QAR_NAPI_LOAD(get_value_string_utf8, "napi_get_value_string_utf8")
    QAR_NAPI_LOAD(get_value_double, "napi_get_value_double")
    QAR_NAPI_LOAD(create_function, "napi_create_function")
    QAR_NAPI_LOAD(create_object, "napi_create_object")
    QAR_NAPI_LOAD(create_string_utf8, "napi_create_string_utf8")
    QAR_NAPI_LOAD(create_double, "napi_create_double")
    QAR_NAPI_LOAD(create_uint32, "napi_create_uint32")
    QAR_NAPI_LOAD(get_boolean, "napi_get_boolean")
    QAR_NAPI_LOAD(set_named_property, "napi_set_named_property")
#undef QAR_NAPI_LOAD
    *api = candidate;
    return candidate.get_cb_info && candidate.get_named_property && candidate.typeof_value && candidate.get_value_string_utf8 &&
        candidate.get_value_double && candidate.create_function && candidate.create_object &&
        candidate.create_double && candidate.create_uint32 && candidate.get_boolean &&
        candidate.set_named_property;
}

} // namespace qar

extern "C" __declspec(dllexport) napi_value napi_register_module_v1(napi_env env, napi_value exports) {
    if (!qar::InitializeNapiApi(&qar::g_napi)) return exports;
    qar::g_napiReady = true;
    napi_value install = nullptr;
    napi_value status = nullptr;
    if (qar::g_napi.create_function(env, "install", NAPI_AUTO_LENGTH, &qar::InstallCallback,
                                    nullptr, &install) != 0 ||
        qar::g_napi.create_function(env, "status", NAPI_AUTO_LENGTH, &qar::StatusCallback,
                                    nullptr, &status) != 0) return exports;
    qar::g_napi.set_named_property(env, exports, "install", install);
    qar::g_napi.set_named_property(env, exports, "status", status);
    return exports;
}
