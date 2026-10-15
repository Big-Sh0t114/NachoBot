#pragma once

#include <cstddef>
#include <cstdint>
#include <cmath>

struct napi_env__;
struct napi_value__;
struct napi_callback_info__;
using napi_env = napi_env__*;
using napi_value = napi_value__*;
using napi_callback_info = napi_callback_info__*;
using napi_status = int32_t;
using napi_callback = napi_value (*)(napi_env, napi_callback_info);
using napi_valuetype = int32_t;
static constexpr napi_valuetype kNapiNumberType = 3; // Public napi_valuetype::napi_number.
static constexpr size_t NAPI_AUTO_LENGTH = static_cast<size_t>(-1);

inline bool ParseSafeGeneration(bool present, double value, uint64_t* output) {
    if (output) *output = 0;
    if (!present || !output || !std::isfinite(value) || value <= 0 ||
        std::floor(value) != value || value > 9007199254740991.0) return false;
    *output = static_cast<uint64_t>(value);
    return *output != 0;
}

struct NapiApi {
    napi_status (*get_cb_info)(napi_env, napi_callback_info, size_t*, napi_value*, napi_value*, void**);
    napi_status (*get_named_property)(napi_env, napi_value, const char*, napi_value*);
    napi_status (*typeof_value)(napi_env, napi_value, napi_valuetype*);
    napi_status (*get_value_string_utf8)(napi_env, napi_value, char*, size_t, size_t*);
    napi_status (*get_value_double)(napi_env, napi_value, double*);
    napi_status (*create_function)(napi_env, const char*, size_t, napi_callback, void*, napi_value*);
    napi_status (*create_object)(napi_env, napi_value*);
    napi_status (*create_string_utf8)(napi_env, const char*, size_t, napi_value*);
    napi_status (*create_double)(napi_env, double, napi_value*);
    napi_status (*create_uint32)(napi_env, uint32_t, napi_value*);
    napi_status (*get_boolean)(napi_env, bool, napi_value*);
    napi_status (*set_named_property)(napi_env, napi_value, const char*, napi_value);
};

namespace qar {
bool InitializeNapiApi(NapiApi* api);
}
