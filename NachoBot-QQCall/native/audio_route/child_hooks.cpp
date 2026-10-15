#include <initguid.h>
#include "audio_route_guard.h"
#include "child_activation_policy.h"
#include "hook_runtime.h"
#include "runtime_internal.h"

#include <audioclient.h>
#include <dsound.h>
#include <mmdeviceapi.h>
#include <mmsystem.h>
#include <propsys.h>

#include <atomic>
#include <cwchar>

namespace qar {
namespace {

using CoCreateInstanceFn = HRESULT (WINAPI*)(REFCLSID, IUnknown*, DWORD, REFIID, LPVOID*);
using CoCreateInstanceExFn = HRESULT (WINAPI*)(REFCLSID, IUnknown*, DWORD, COSERVERINFO*, DWORD, MULTI_QI*);
using CoGetClassObjectFn = HRESULT (WINAPI*)(REFCLSID, DWORD, COSERVERINFO*, REFIID, LPVOID*);
using ActivateAudioInterfaceAsyncFn = HRESULT (WINAPI*)(LPCWSTR, REFIID, PROPVARIANT*,
    IActivateAudioInterfaceCompletionHandler*, IActivateAudioInterfaceAsyncOperation**);
using DirectSoundCreateFn = HRESULT (WINAPI*)(LPCGUID, LPDIRECTSOUND*, LPUNKNOWN);
using DirectSoundCreate8Fn = HRESULT (WINAPI*)(LPCGUID, LPDIRECTSOUND8*, LPUNKNOWN);
using DirectSoundCaptureCreateFn = HRESULT (WINAPI*)(LPCGUID, LPDIRECTSOUNDCAPTURE*, LPUNKNOWN);
using DirectSoundCaptureCreate8Fn = HRESULT (WINAPI*)(LPCGUID, LPDIRECTSOUNDCAPTURE8*, LPUNKNOWN);
using DirectSoundFullDuplexCreateFn = HRESULT (WINAPI*)(LPCGUID, LPCGUID, LPCDSCBUFFERDESC,
    LPCDSBUFFERDESC, HWND, DWORD, LPDIRECTSOUNDFULLDUPLEX*, LPDIRECTSOUNDCAPTUREBUFFER8*,
    LPDIRECTSOUNDBUFFER8*, LPUNKNOWN);
using WaveInOpenFn = MMRESULT (WINAPI*)(LPHWAVEIN, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD);
using WaveOutOpenFn = MMRESULT (WINAPI*)(LPHWAVEOUT, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD);

thread_local uint32_t g_hookDepth = 0;
std::atomic<bool> g_hooksInstalled(false);
std::atomic_flag g_installLock = ATOMIC_FLAG_INIT;
HookBackend* g_backend = nullptr;
HookRequest g_requests[11] = {};

class ReentryScope final {
public:
    ReentryScope() : outer_(g_hookDepth++ == 0) {
        if (!outer_) CountReentry();
    }
    ~ReentryScope() { --g_hookDepth; }
    bool outer() const { return outer_; }
private:
    bool outer_;
};

bool IsEnumeratorClass(REFCLSID clsid) { return clsid == __uuidof(MMDeviceEnumerator); }
bool IsSupportedEnumeratorIid(REFIID iid) {
    return iid == IID_IUnknown || iid == __uuidof(IMMDeviceEnumerator);
}

template <typename T> class ScopedInterface final {
public:
    T* value = nullptr;
    ~ScopedInterface() { if (value) value->Release(); }
};

HRESULT ResolvePinnedRenderGuid(GUID* guid) {
    if (!guid) return E_POINTER;
    *guid = GUID_NULL;
    const ConfigV1* config = ActiveChildConfig();
    if (!config || !config->renderEndpointId[0]) return E_ACCESSDENIED;
    const HRESULT init = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(init) && init != RPC_E_CHANGED_MODE) return init;
    struct Apartment {
        bool owned;
        ~Apartment() { if (owned) CoUninitialize(); }
    } apartment{SUCCEEDED(init)};
    ScopedInterface<IMMDeviceEnumerator> enumerator;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_INPROC_SERVER,
        __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(&enumerator.value));
    if (FAILED(hr) || !enumerator.value) return FAILED(hr) ? hr : E_ACCESSDENIED;
    hr = ValidateConfiguredEndpoints(enumerator.value, config);
    if (FAILED(hr)) return hr;
    ScopedInterface<IMMDevice> target;
    hr = enumerator.value->GetDevice(config->renderEndpointId, &target.value);
    if (FAILED(hr) || !target.value) return FAILED(hr) ? hr : E_ACCESSDENIED;
    ScopedInterface<IPropertyStore> properties;
    hr = target.value->OpenPropertyStore(STGM_READ, &properties.value);
    if (FAILED(hr) || !properties.value) return FAILED(hr) ? hr : E_ACCESSDENIED;
    PROPVARIANT value;
    PropVariantInit(&value);
    hr = properties.value->GetValue(PKEY_AudioEndpoint_GUID, &value);
    if (SUCCEEDED(hr)) {
        if (value.vt != VT_LPWSTR || !value.pwszVal ||
            wcsnlen_s(value.pwszVal, 39) != 38) hr = E_ACCESSDENIED;
        else hr = CLSIDFromString(value.pwszVal, guid);
    }
    PropVariantClear(&value);
    if (FAILED(hr) || *guid == GUID_NULL || *guid == DSDEVID_DefaultPlayback ||
        *guid == DSDEVID_DefaultCapture || *guid == DSDEVID_DefaultVoicePlayback ||
        *guid == DSDEVID_DefaultVoiceCapture) {
        *guid = GUID_NULL;
        return FAILED(hr) ? hr : E_ACCESSDENIED;
    }
    return S_OK;
}

thread_local uint32_t g_directSoundDepth = 0;
class DirectSoundScope final {
public:
    DirectSoundScope() { ++g_directSoundDepth; }
    ~DirectSoundScope() { --g_directSoundDepth; }
};

template <typename T, typename Function>
HRESULT CreatePinnedDirectSound(T** sound, IUnknown* outer, Function original) {
    if (!sound) return E_POINTER;
    *sound = nullptr;
    if (outer) return CLASS_E_NOAGGREGATION;
    if (!original || g_directSoundDepth >= 4) {
        RecordDiagnostic(DiagnosticStage::direct_sound_render, E_ACCESSDENIED);
        CountRejectedActivation();
        return E_ACCESSDENIED;
    }
    DirectSoundScope scope;
    GUID target;
    HRESULT hr = ResolvePinnedRenderGuid(&target);
    if (SUCCEEDED(hr)) hr = original(&target, sound, nullptr);
    if (FAILED(hr) && *sound) { (*sound)->Release(); *sound = nullptr; }
    if (SUCCEEDED(hr) && !*sound) hr = E_ACCESSDENIED;
    RecordDiagnostic(DiagnosticStage::direct_sound_render, hr);
    if (FAILED(hr)) CountRejectedActivation();
    return hr;
}

extern "C" HRESULT WINAPI GuardCoCreateInstance(
    REFCLSID clsid, IUnknown* outer, DWORD clsctx, REFIID iid, LPVOID* result) {
    const auto original = reinterpret_cast<CoCreateInstanceFn>(g_requests[0].original);
    if (!original) return CO_E_NOTINITIALIZED;
    const bool target = IsEnumeratorClass(clsid);
    if (target) RecordDiagnostic(DiagnosticStage::create_enumerator, E_PENDING);
    if (DenyReentrantEnumeratorActivation(g_hookDepth != 0, target)) {
        if (result) *result = nullptr;
        return E_ACCESSDENIED;
    }
    if (!target) {
        ReentryScope scope;
        return original(clsid, outer, clsctx, iid, result);
    }
    if (result) *result = nullptr;
    if (!result) return E_POINTER;
    if (outer) return CLASS_E_NOAGGREGATION;
    if (!IsSupportedEnumeratorIid(iid)) return E_NOINTERFACE;
    // CLSCTX_ALL is a normal local caller's preference mask. Resolve this
    // known class strictly in-process rather than rejecting the whole mask.
    if ((clsctx & CLSCTX_INPROC_SERVER) == 0) return E_ACCESSDENIED;
    const DWORD localContext = clsctx & ~(CLSCTX_LOCAL_SERVER | CLSCTX_REMOTE_SERVER | CLSCTX_INPROC_HANDLER);
    ReentryScope scope;
    IUnknown* raw = nullptr;
    HRESULT hr = original(clsid, nullptr, localContext, IID_IUnknown, reinterpret_cast<void**>(&raw));
    if (FAILED(hr) || !raw) return FAILED(hr) ? hr : E_ACCESSDENIED;
    const ConfigV1* config = ActiveChildConfig();
    hr = config ? WrapRequestedEnumerator(iid, raw, config, result) : E_ACCESSDENIED;
    RecordDiagnostic(DiagnosticStage::wrap_enumerator, hr);
    raw->Release();
    if (FAILED(hr)) *result = nullptr;
    return hr;
}

extern "C" HRESULT WINAPI GuardCoCreateInstanceEx(
    REFCLSID clsid, IUnknown* outer, DWORD clsctx, COSERVERINFO* serverInfo,
    DWORD count, MULTI_QI* results) {
    const auto original = reinterpret_cast<CoCreateInstanceExFn>(g_requests[1].original);
    if (!original) return CO_E_NOTINITIALIZED;
    const bool target = IsEnumeratorClass(clsid);
    if (target) RecordDiagnostic(DiagnosticStage::create_enumerator_ex, E_PENDING);
    if (DenyReentrantEnumeratorActivation(g_hookDepth != 0, target)) {
        if (!results || count == 0 || count > 64) return E_INVALIDARG;
        for (DWORD i = 0; i < count; ++i) {
            results[i].pItf = nullptr;
            results[i].hr = E_ACCESSDENIED;
        }
        return E_ACCESSDENIED;
    }
    if (!target) {
        ReentryScope scope;
        return original(clsid, outer, clsctx, serverInfo, count, results);
    }
    if (!results || count == 0 || count > 64) return E_INVALIDARG;
    for (DWORD i = 0; i < count; ++i) {
        results[i].pItf = nullptr;
        results[i].hr = E_NOINTERFACE;
        if (!results[i].pIID) {
            results[i].hr = E_POINTER;
            return E_INVALIDARG;
        }
    }
    if (outer) return CLASS_E_NOAGGREGATION;
    if (serverInfo || (clsctx & CLSCTX_INPROC_SERVER) == 0) return E_ACCESSDENIED;
    const DWORD localContext = clsctx & ~(CLSCTX_LOCAL_SERVER | CLSCTX_REMOTE_SERVER | CLSCTX_INPROC_HANDLER);
    bool supported = false;
    for (DWORD i = 0; i < count; ++i) supported = supported || IsSupportedEnumeratorIid(*results[i].pIID);
    if (!supported) return E_NOINTERFACE;
    ReentryScope scope;
    MULTI_QI rawQuery = {};
    rawQuery.pIID = &IID_IUnknown;
    HRESULT hr = original(clsid, nullptr, localContext, nullptr, 1, &rawQuery);
    IUnknown* raw = rawQuery.pItf;
    if (FAILED(hr) || FAILED(rawQuery.hr) || !raw) {
        if (raw) raw->Release();
        const HRESULT failure = FAILED(hr) ? hr : (FAILED(rawQuery.hr) ? rawQuery.hr : E_ACCESSDENIED);
        for (DWORD i = 0; i < count; ++i) results[i].hr = failure;
        return failure;
    }
    const ConfigV1* config = ActiveChildConfig();
    IMMDeviceEnumerator* wrapped = nullptr;
    HRESULT wrapHr = config ? WrapEnumerator(raw, config, &wrapped) : E_ACCESSDENIED;
    RecordDiagnostic(DiagnosticStage::wrap_enumerator, wrapHr);
    raw->Release();
    if (FAILED(wrapHr) || !wrapped) {
        for (DWORD i = 0; i < count; ++i) results[i].hr = FAILED(wrapHr) ? wrapHr : E_ACCESSDENIED;
        return FAILED(wrapHr) ? wrapHr : E_ACCESSDENIED;
    }
    HRESULT aggregate = S_OK;
    for (DWORD i = 0; i < count; ++i) {
        if (!IsSupportedEnumeratorIid(*results[i].pIID)) {
            results[i].hr = E_NOINTERFACE;
            if (SUCCEEDED(aggregate)) aggregate = E_NOINTERFACE;
            continue;
        }
        results[i].hr = wrapped->QueryInterface(*results[i].pIID, reinterpret_cast<void**>(&results[i].pItf));
        if (FAILED(results[i].hr) && SUCCEEDED(aggregate)) aggregate = results[i].hr;
    }
    wrapped->Release();
    return aggregate;
}

extern "C" HRESULT WINAPI GuardCoGetClassObject(
    REFCLSID clsid, DWORD clsctx, COSERVERINFO* serverInfo, REFIID iid, LPVOID* result) {
    const auto original = reinterpret_cast<CoGetClassObjectFn>(g_requests[2].original);
    if (!original) return CO_E_NOTINITIALIZED;
    const bool target = IsEnumeratorClass(clsid);
    if (target) RecordDiagnostic(DiagnosticStage::class_factory, E_ACCESSDENIED);
    if (DenyReentrantEnumeratorActivation(g_hookDepth != 0, target)) {
        if (result) *result = nullptr;
        return E_ACCESSDENIED;
    }
    if (!target) {
        ReentryScope scope;
        return original(clsid, clsctx, serverInfo, iid, result);
    }
    if (result) *result = nullptr;
    if (!result) return E_POINTER;
    return E_ACCESSDENIED;
}

extern "C" HRESULT WINAPI GuardActivateAudioInterfaceAsync(
    LPCWSTR, REFIID, PROPVARIANT*, IActivateAudioInterfaceCompletionHandler*,
    IActivateAudioInterfaceAsyncOperation** operation) {
    if (!operation) return E_POINTER;
    *operation = nullptr;
    RecordDiagnostic(DiagnosticStage::async_audio, E_ACCESSDENIED);
    CountRejectedActivation();
    return E_ACCESSDENIED;
}

extern "C" HRESULT WINAPI GuardDirectSoundCreate(
    LPCGUID, LPDIRECTSOUND* sound, LPUNKNOWN outer) {
    return CreatePinnedDirectSound(sound, outer,
        reinterpret_cast<DirectSoundCreateFn>(g_requests[4].original));
}
extern "C" HRESULT WINAPI GuardDirectSoundCreate8(
    LPCGUID, LPDIRECTSOUND8* sound, LPUNKNOWN outer) {
    return CreatePinnedDirectSound(sound, outer,
        reinterpret_cast<DirectSoundCreate8Fn>(g_requests[5].original));
}
extern "C" HRESULT WINAPI GuardDirectSoundCaptureCreate(
    LPCGUID, LPDIRECTSOUNDCAPTURE* capture, LPUNKNOWN) {
    if (capture) *capture = nullptr;
    RecordDiagnostic(DiagnosticStage::direct_sound_capture, DSERR_ACCESSDENIED);
    CountRejectedActivation();
    return DSERR_ACCESSDENIED;
}
extern "C" HRESULT WINAPI GuardDirectSoundCaptureCreate8(
    LPCGUID, LPDIRECTSOUNDCAPTURE8* capture, LPUNKNOWN) {
    if (capture) *capture = nullptr;
    RecordDiagnostic(DiagnosticStage::direct_sound_capture, DSERR_ACCESSDENIED);
    CountRejectedActivation();
    return DSERR_ACCESSDENIED;
}
extern "C" HRESULT WINAPI GuardDirectSoundFullDuplexCreate(
    LPCGUID, LPCGUID, LPCDSCBUFFERDESC, LPCDSBUFFERDESC, HWND, DWORD,
    LPDIRECTSOUNDFULLDUPLEX* duplex, LPDIRECTSOUNDCAPTUREBUFFER8* capture,
    LPDIRECTSOUNDBUFFER8* render, LPUNKNOWN) {
    if (duplex) *duplex = nullptr;
    if (capture) *capture = nullptr;
    if (render) *render = nullptr;
    RecordDiagnostic(DiagnosticStage::direct_sound_duplex, DSERR_ACCESSDENIED);
    CountRejectedActivation();
    return DSERR_ACCESSDENIED;
}
extern "C" MMRESULT WINAPI GuardWaveInOpen(
    LPHWAVEIN wave, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD) {
    if (wave) *wave = nullptr;
    RecordDiagnostic(DiagnosticStage::wave_capture, MMSYSERR_NOTENABLED);
    CountRejectedActivation();
    return MMSYSERR_NOTENABLED;
}
extern "C" MMRESULT WINAPI GuardWaveOutOpen(
    LPHWAVEOUT wave, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD) {
    if (wave) *wave = nullptr;
    RecordDiagnostic(DiagnosticStage::wave_render, MMSYSERR_NOTENABLED);
    CountRejectedActivation();
    return MMSYSERR_NOTENABLED;
}

} // namespace

bool InstallChildApiHooks() {
    if (g_hooksInstalled.load(std::memory_order_acquire)) return true;
    while (g_installLock.test_and_set(std::memory_order_acquire)) Sleep(0);
    if (g_hooksInstalled.load(std::memory_order_relaxed)) {
        g_installLock.clear(std::memory_order_release);
        return true;
    }
    const HookRequest requested[] = {
        {L"combase.dll", "CoCreateInstance", reinterpret_cast<void*>(&GuardCoCreateInstance)},
        {L"combase.dll", "CoCreateInstanceEx", reinterpret_cast<void*>(&GuardCoCreateInstanceEx)},
        {L"combase.dll", "CoGetClassObject", reinterpret_cast<void*>(&GuardCoGetClassObject)},
        {L"mmdevapi.dll", "ActivateAudioInterfaceAsync", reinterpret_cast<void*>(&GuardActivateAudioInterfaceAsync)},
        {L"dsound.dll", "DirectSoundCreate", reinterpret_cast<void*>(&GuardDirectSoundCreate)},
        {L"dsound.dll", "DirectSoundCreate8", reinterpret_cast<void*>(&GuardDirectSoundCreate8)},
        {L"dsound.dll", "DirectSoundCaptureCreate", reinterpret_cast<void*>(&GuardDirectSoundCaptureCreate)},
        {L"dsound.dll", "DirectSoundCaptureCreate8", reinterpret_cast<void*>(&GuardDirectSoundCaptureCreate8)},
        {L"dsound.dll", "DirectSoundFullDuplexCreate", reinterpret_cast<void*>(&GuardDirectSoundFullDuplexCreate)},
        {L"winmm.dll", "waveInOpen", reinterpret_cast<void*>(&GuardWaveInOpen)},
        {L"winmm.dll", "waveOutOpen", reinterpret_cast<void*>(&GuardWaveOutOpen)},
    };
    for (size_t i = 0; i < ARRAYSIZE(requested); ++i) g_requests[i] = requested[i];
    g_backend = CreateMinHookBackend();
    const bool ok = g_backend && InstallHookSet(g_backend, g_requests, ARRAYSIZE(g_requests));
    if (ok) {
        g_hooksInstalled.store(true, std::memory_order_release);
        CountInstalledHooks(ARRAYSIZE(g_requests));
    } else if (g_backend) {
        DestroyMinHookBackend(g_backend);
        g_backend = nullptr;
    }
    g_installLock.clear(std::memory_order_release);
    return ok;
}

#if defined(QAR_TESTING)
using QarTestCoCreateOriginal = HRESULT (WINAPI*)(REFCLSID, IUnknown*, DWORD, REFIID, LPVOID*);
using QarTestCoCreateExOriginal = HRESULT (WINAPI*)(REFCLSID, IUnknown*, DWORD, COSERVERINFO*, DWORD, MULTI_QI*);
using QarTestCoGetClassObjectOriginal = HRESULT (WINAPI*)(REFCLSID, DWORD, COSERVERINFO*, REFIID, LPVOID*);
extern "C" void QarTestSetCoCreateOriginal(void* value) { g_requests[0].original = value; }
extern "C" void QarTestSetCoCreateExOriginal(void* value) { g_requests[1].original = value; }
extern "C" void QarTestSetCoGetClassObjectOriginal(void* value) { g_requests[2].original = value; }
extern "C" HRESULT WINAPI QarTestCallCoCreate(REFCLSID clsid, IUnknown* outer, DWORD clsctx,
    REFIID iid, LPVOID* result) { return GuardCoCreateInstance(clsid, outer, clsctx, iid, result); }
extern "C" HRESULT WINAPI QarTestCallCoCreateEx(REFCLSID clsid, IUnknown* outer, DWORD clsctx,
    COSERVERINFO* serverInfo, DWORD count, MULTI_QI* results) {
    return GuardCoCreateInstanceEx(clsid, outer, clsctx, serverInfo, count, results);
}
extern "C" HRESULT WINAPI QarTestCallCoGetClassObject(REFCLSID clsid, DWORD clsctx,
    COSERVERINFO* serverInfo, REFIID iid, LPVOID* result) {
    return GuardCoGetClassObject(clsid, clsctx, serverInfo, iid, result);
}
extern "C" HRESULT WINAPI QarTestCallActivateAudioInterfaceAsync(LPCWSTR deviceId, REFIID iid,
    PROPVARIANT* parameters, IActivateAudioInterfaceCompletionHandler* completionHandler,
    IActivateAudioInterfaceAsyncOperation** operation) {
    return GuardActivateAudioInterfaceAsync(deviceId, iid, parameters, completionHandler, operation);
}
extern "C" HRESULT WINAPI QarTestCallDirectSoundCreate(LPCGUID guid, LPDIRECTSOUND* sound, LPUNKNOWN outer) {
    return GuardDirectSoundCreate(guid, sound, outer);
}
extern "C" HRESULT WINAPI QarTestCallDirectSoundCreate8(LPCGUID guid, LPDIRECTSOUND8* sound, LPUNKNOWN outer) {
    return GuardDirectSoundCreate8(guid, sound, outer);
}
extern "C" HRESULT WINAPI QarTestCallDirectSoundCaptureCreate(LPCGUID guid,
    LPDIRECTSOUNDCAPTURE* capture, LPUNKNOWN outer) {
    return GuardDirectSoundCaptureCreate(guid, capture, outer);
}
extern "C" HRESULT WINAPI QarTestCallDirectSoundCaptureCreate8(LPCGUID guid,
    LPDIRECTSOUNDCAPTURE8* capture, LPUNKNOWN outer) {
    return GuardDirectSoundCaptureCreate8(guid, capture, outer);
}
extern "C" HRESULT WINAPI QarTestCallDirectSoundFullDuplexCreate(LPCGUID captureGuid, LPCGUID renderGuid,
    LPCDSCBUFFERDESC captureDesc, LPCDSBUFFERDESC renderDesc, HWND window, DWORD level,
    LPDIRECTSOUNDFULLDUPLEX* duplex, LPDIRECTSOUNDCAPTUREBUFFER8* capture,
    LPDIRECTSOUNDBUFFER8* render, LPUNKNOWN outer) {
    return GuardDirectSoundFullDuplexCreate(captureGuid, renderGuid, captureDesc, renderDesc,
        window, level, duplex, capture, render, outer);
}
extern "C" MMRESULT WINAPI QarTestCallWaveInOpen(LPHWAVEIN wave, UINT device,
    LPCWAVEFORMATEX format, DWORD_PTR callback, DWORD_PTR instance, DWORD flags) {
    return GuardWaveInOpen(wave, device, format, callback, instance, flags);
}
extern "C" MMRESULT WINAPI QarTestCallWaveOutOpen(LPHWAVEOUT wave, UINT device,
    LPCWAVEFORMATEX format, DWORD_PTR callback, DWORD_PTR instance, DWORD flags) {
    return GuardWaveOutOpen(wave, device, format, callback, instance, flags);
}
#endif

} // namespace qar
