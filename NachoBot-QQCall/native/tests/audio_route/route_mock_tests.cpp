#include "../../audio_route/audio_route_guard.h"
#include "../../audio_route/child_activation_policy.h"
#include "../../audio_route/cfg_metadata.h"
#include "../../audio_route/hook_runtime.h"
#include "../../audio_route/launch_barrier.h"
#include "../../audio_route/launch_classification.h"
#include "../../audio_route/loader_policy.h"
#include "../../audio_route/media_command.h"
#include "../../audio_route/node_api_compat.h"
#include "../../audio_route/parent_hooks.h"

#include <audioclient.h>
#include <dsound.h>
#include <mmsystem.h>
#include <propsys.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

extern "C" void QarTestSetCoCreateOriginal(void* value);
extern "C" void QarTestSetCoCreateExOriginal(void* value);
extern "C" void QarTestSetCoGetClassObjectOriginal(void* value);
extern "C" HRESULT WINAPI QarTestCallCoCreate(REFCLSID, IUnknown*, DWORD, REFIID, LPVOID*);
extern "C" HRESULT WINAPI QarTestCallCoCreateEx(REFCLSID, IUnknown*, DWORD, COSERVERINFO*, DWORD, MULTI_QI*);
extern "C" HRESULT WINAPI QarTestCallCoGetClassObject(REFCLSID, DWORD, COSERVERINFO*, REFIID, LPVOID*);
extern "C" HRESULT WINAPI QarTestCallActivateAudioInterfaceAsync(LPCWSTR, REFIID, PROPVARIANT*,
    IActivateAudioInterfaceCompletionHandler*, IActivateAudioInterfaceAsyncOperation**);
extern "C" HRESULT WINAPI QarTestCallDirectSoundCreate(LPCGUID, LPDIRECTSOUND*, LPUNKNOWN);
extern "C" HRESULT WINAPI QarTestCallDirectSoundCreate8(LPCGUID, LPDIRECTSOUND8*, LPUNKNOWN);
extern "C" HRESULT WINAPI QarTestCallDirectSoundCaptureCreate(LPCGUID, LPDIRECTSOUNDCAPTURE*, LPUNKNOWN);
extern "C" HRESULT WINAPI QarTestCallDirectSoundCaptureCreate8(LPCGUID, LPDIRECTSOUNDCAPTURE8*, LPUNKNOWN);
extern "C" HRESULT WINAPI QarTestCallDirectSoundFullDuplexCreate(LPCGUID, LPCGUID, LPCDSCBUFFERDESC,
    LPCDSBUFFERDESC, HWND, DWORD, LPDIRECTSOUNDFULLDUPLEX*, LPDIRECTSOUNDCAPTUREBUFFER8*,
    LPDIRECTSOUNDBUFFER8*, LPUNKNOWN);
extern "C" MMRESULT WINAPI QarTestCallWaveInOpen(LPHWAVEIN, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD);
extern "C" MMRESULT WINAPI QarTestCallWaveOutOpen(LPHWAVEOUT, UINT, LPCWAVEFORMATEX, DWORD_PTR, DWORD_PTR, DWORD);

namespace qar {
namespace {
StatusV1 g_testStatus = {};
}
const ConfigV1* ActiveChildConfig() { return nullptr; }
StatusV1* ActiveChildStatus() { return &g_testStatus; }
void CountInstalledHooks(uint64_t value) { g_testStatus.installedHookCount += value; }
void CountReentry() { ++g_testStatus.reentryCount; }
void CountRejectedActivation() { ++g_testStatus.rejectedActivationCount; }
}

namespace {

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct EndpointStats {
    std::atomic<uint32_t> activationCount{0};
};

enum class PropertyMode : uint32_t { ui4, vectorUi4, lpwstr, vectorLpwstr, blob, emptyBlob, oversizedBlob, nullBlob, unknown, byrefUnknown, arrayUnknown };
std::atomic<PropertyMode> g_propertyMode(PropertyMode::ui4);

class FakeAudio final : public IUnknown {
public:
    FakeAudio() : refs_(1) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (iid == IID_IUnknown || iid == __uuidof(IAudioClient) ||
            iid == __uuidof(IAudioClient2) || iid == __uuidof(IAudioClient3)) {
            *out = static_cast<IUnknown*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --refs_;
        if (!remaining) delete this;
        return remaining;
    }
private:
    std::atomic<ULONG> refs_;
};

class FakePropertyStore final : public IPropertyStore {
public:
    explicit FakePropertyStore(IUnknown* escapeTarget)
        : refs_(1), marshaler_(nullptr), borrowed_(escapeTarget) {
        if (borrowed_) borrowed_->AddRef();
        CoCreateFreeThreadedMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~FakePropertyStore() {
        if (marshaler_) marshaler_->Release();
        if (borrowed_) borrowed_->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (iid == IID_IUnknown || iid == __uuidof(IPropertyStore)) {
            *out = static_cast<IPropertyStore*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == IID_IMarshal && marshaler_) return marshaler_->QueryInterface(iid, out);
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --refs_;
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE GetCount(DWORD* count) override {
        if (count) *count = 1;
        return count ? S_OK : E_POINTER;
    }
    HRESULT STDMETHODCALLTYPE GetAt(DWORD index, PROPERTYKEY* key) override {
        if (key) *key = PROPERTYKEY{};
        if (!key) return E_POINTER;
        return index == 0 ? S_OK : E_INVALIDARG;
    }
    HRESULT STDMETHODCALLTYPE GetValue(REFPROPERTYKEY, PROPVARIANT* value) override {
        if (!value) return E_POINTER;
        PropVariantInit(value);
        switch (g_propertyMode.load(std::memory_order_relaxed)) {
        case PropertyMode::ui4:
            value->vt = VT_UI4;
            value->ulVal = 17;
            return S_OK;
        case PropertyMode::vectorUi4:
            value->vt = VT_VECTOR | VT_UI4;
            value->caul.cElems = 2;
            value->caul.pElems = static_cast<ULONG*>(CoTaskMemAlloc(sizeof(ULONG) * 2));
            if (!value->caul.pElems) return E_OUTOFMEMORY;
            value->caul.pElems[0] = 17;
            value->caul.pElems[1] = 23;
            return S_OK;
        case PropertyMode::lpwstr: {
            value->vt = VT_LPWSTR;
            constexpr wchar_t text[] = L"mock endpoint metadata";
            value->pwszVal = static_cast<LPWSTR>(CoTaskMemAlloc(sizeof(text)));
            if (!value->pwszVal) return E_OUTOFMEMORY;
            memcpy(value->pwszVal, text, sizeof(text));
            return S_OK;
        }
        case PropertyMode::vectorLpwstr: {
            value->vt = VT_VECTOR | VT_LPWSTR;
            value->calpwstr.cElems = 2;
            value->calpwstr.pElems = static_cast<LPWSTR*>(CoTaskMemAlloc(sizeof(LPWSTR) * 2));
            if (!value->calpwstr.pElems) return E_OUTOFMEMORY;
            value->calpwstr.pElems[0] = static_cast<LPWSTR>(CoTaskMemAlloc(sizeof(L"mock")));
            value->calpwstr.pElems[1] = static_cast<LPWSTR>(CoTaskMemAlloc(sizeof(L"metadata")));
            if (!value->calpwstr.pElems[0] || !value->calpwstr.pElems[1]) return E_OUTOFMEMORY;
            memcpy(value->calpwstr.pElems[0], L"mock", sizeof(L"mock"));
            memcpy(value->calpwstr.pElems[1], L"metadata", sizeof(L"metadata"));
            return S_OK;
        }
        case PropertyMode::blob:
        case PropertyMode::emptyBlob:
        case PropertyMode::oversizedBlob:
        case PropertyMode::nullBlob: {
            const auto mode = g_propertyMode.load(std::memory_order_relaxed);
            value->vt = VT_BLOB;
            value->blob.cbSize = mode == PropertyMode::emptyBlob ? 0 :
                (mode == PropertyMode::oversizedBlob ? 1024u * 1024u + 1 : sizeof(WAVEFORMATEX));
            value->blob.pBlobData = mode == PropertyMode::emptyBlob || mode == PropertyMode::nullBlob ? nullptr :
                static_cast<BYTE*>(CoTaskMemAlloc(value->blob.cbSize));
            if (mode != PropertyMode::emptyBlob && mode != PropertyMode::nullBlob && !value->blob.pBlobData) return E_OUTOFMEMORY;
            if (value->blob.pBlobData) memset(value->blob.pBlobData, 0x17, value->blob.cbSize);
            return S_OK;
        }
        case PropertyMode::unknown:
            value->vt = VT_UNKNOWN;
            value->punkVal = borrowed_;
            if (value->punkVal) value->punkVal->AddRef();
            return S_OK;
        case PropertyMode::byrefUnknown:
            value->vt = VT_BYREF | VT_UNKNOWN;
            value->ppunkVal = &borrowed_;
            return S_OK;
        case PropertyMode::arrayUnknown:
            value->vt = VT_ARRAY | VT_UNKNOWN;
            value->parray = SafeArrayCreateVector(VT_UNKNOWN, 0, 0);
            return value->parray ? S_OK : E_OUTOFMEMORY;
        }
        return E_FAIL;
    }
    HRESULT STDMETHODCALLTYPE SetValue(REFPROPERTYKEY, REFPROPVARIANT) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE Commit() override { return S_OK; }
private:
    std::atomic<ULONG> refs_;
    IUnknown* marshaler_;
    IUnknown* borrowed_;
};

class FakeDevice final : public IMMDevice, public IMMEndpoint {
public:
    FakeDevice(const wchar_t* id, EDataFlow flow, EndpointStats* stats, DWORD state = DEVICE_STATE_ACTIVE)
        : refs_(1), id_(id), flow_(flow), stats_(stats), state_(state), marshaler_(nullptr) {
        CoCreateFreeThreadedMarshaler(static_cast<IUnknown*>(static_cast<IMMDevice*>(this)), &marshaler_);
    }
    ~FakeDevice() { if (marshaler_) marshaler_->Release(); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (iid == IID_IUnknown || iid == __uuidof(IMMDevice)) {
            *out = static_cast<IMMDevice*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == __uuidof(IMMEndpoint)) {
            *out = static_cast<IMMEndpoint*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == IID_IMarshal && marshaler_) return marshaler_->QueryInterface(iid, out);
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --refs_;
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE Activate(REFIID, DWORD, PROPVARIANT*, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (stats_) ++stats_->activationCount;
        FakeAudio* audio = new (std::nothrow) FakeAudio();
        if (!audio) return E_OUTOFMEMORY;
        *out = static_cast<IUnknown*>(audio);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OpenPropertyStore(DWORD access, IPropertyStore** store) override {
        if (store) *store = nullptr;
        if (!store) return E_POINTER;
        if (access != STGM_READ) return STG_E_ACCESSDENIED;
        FakePropertyStore* value = new (std::nothrow)
            FakePropertyStore(static_cast<IUnknown*>(static_cast<IMMDevice*>(this)));
        if (!value) return E_OUTOFMEMORY;
        *store = value;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetId(LPWSTR* id) override {
        if (id) *id = nullptr;
        if (!id) return E_POINTER;
        const size_t bytes = (id_.size() + 1) * sizeof(wchar_t);
        *id = static_cast<LPWSTR>(CoTaskMemAlloc(bytes));
        if (!*id) return E_OUTOFMEMORY;
        memcpy(*id, id_.c_str(), bytes);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetState(DWORD* state) override {
        if (state) *state = state_;
        return state ? S_OK : E_POINTER;
    }
    HRESULT STDMETHODCALLTYPE GetDataFlow(EDataFlow* flow) override {
        if (flow) *flow = flow_;
        return flow ? S_OK : E_POINTER;
    }
    const std::wstring& id() const { return id_; }
    EDataFlow flow() const { return flow_; }
private:
    std::atomic<ULONG> refs_;
    std::wstring id_;
    EDataFlow flow_;
    EndpointStats* stats_;
    DWORD state_;
    IUnknown* marshaler_;
};

class FakeCollection final : public IMMDeviceCollection {
public:
    explicit FakeCollection(std::vector<FakeDevice*> devices) : refs_(1), devices_(std::move(devices)), marshaler_(nullptr) {
        for (FakeDevice* device : devices_) device->AddRef();
        CoCreateFreeThreadedMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~FakeCollection() {
        if (marshaler_) marshaler_->Release();
        for (FakeDevice* device : devices_) device->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (iid == IID_IUnknown || iid == __uuidof(IMMDeviceCollection)) {
            *out = static_cast<IMMDeviceCollection*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == IID_IMarshal && marshaler_) return marshaler_->QueryInterface(iid, out);
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --refs_;
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE GetCount(UINT* count) override {
        if (count) *count = static_cast<UINT>(devices_.size());
        return count ? S_OK : E_POINTER;
    }
    HRESULT STDMETHODCALLTYPE Item(UINT index, IMMDevice** device) override {
        if (device) *device = nullptr;
        if (!device) return E_POINTER;
        if (index >= devices_.size()) return E_INVALIDARG;
        *device = devices_[index];
        (*device)->AddRef();
        return S_OK;
    }
private:
    std::atomic<ULONG> refs_;
    std::vector<FakeDevice*> devices_;
    IUnknown* marshaler_;
};

class FakeEnumerator final : public IMMDeviceEnumerator {
public:
    explicit FakeEnumerator(std::vector<FakeDevice*> devices) : refs_(1), devices_(std::move(devices)), marshaler_(nullptr) {
        for (FakeDevice* device : devices_) device->AddRef();
        CoCreateFreeThreadedMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~FakeEnumerator() {
        if (marshaler_) marshaler_->Release();
        for (FakeDevice* device : devices_) device->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (out) *out = nullptr;
        if (!out) return E_POINTER;
        if (iid == IID_IUnknown || iid == __uuidof(IMMDeviceEnumerator)) {
            *out = static_cast<IMMDeviceEnumerator*>(this);
            AddRef();
            return S_OK;
        }
        if (iid == IID_IMarshal && marshaler_) return marshaler_->QueryInterface(iid, out);
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining = --refs_;
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE EnumAudioEndpoints(EDataFlow flow, DWORD stateMask,
                                                  IMMDeviceCollection** devices) override {
        if (devices) *devices = nullptr;
        if (!devices) return E_POINTER;
        std::vector<FakeDevice*> selected;
        for (FakeDevice* device : devices_) {
            if (flow != eAll && flow != device->flow()) continue;
            DWORD state = 0;
            device->GetState(&state);
            if ((stateMask & state) != 0) selected.push_back(device);
        }
        FakeCollection* collection = new (std::nothrow) FakeCollection(std::move(selected));
        if (!collection) return E_OUTOFMEMORY;
        *devices = collection;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetDefaultAudioEndpoint(EDataFlow flow, ERole, IMMDevice** endpoint) override {
        if (endpoint) *endpoint = nullptr;
        if (!endpoint) return E_POINTER;
        for (FakeDevice* device : devices_) {
            if (device->flow() == flow && device->id().find(L"mock://physical/") == 0) {
                *endpoint = device;
                device->AddRef();
                return S_OK;
            }
        }
        return E_NOTFOUND;
    }
    HRESULT STDMETHODCALLTYPE GetDevice(LPCWSTR id, IMMDevice** device) override {
        if (device) *device = nullptr;
        if (!id || !device) return E_POINTER;
        for (FakeDevice* item : devices_) {
            if (item->id() == id) {
                *device = item;
                item->AddRef();
                return S_OK;
            }
        }
        return E_NOTFOUND;
    }
    HRESULT STDMETHODCALLTYPE RegisterEndpointNotificationCallback(IMMNotificationClient* client) override {
        notificationClient = client;
        return notificationResult;
    }
    HRESULT STDMETHODCALLTYPE UnregisterEndpointNotificationCallback(IMMNotificationClient* client) override {
        if (client != notificationClient) return E_INVALIDARG;
        notificationClient = nullptr;
        return notificationResult;
    }
    IMMNotificationClient* notificationClient = nullptr;
    HRESULT notificationResult = S_OK;
private:
    std::atomic<ULONG> refs_;
    std::vector<FakeDevice*> devices_;
    IUnknown* marshaler_;
};

std::wstring ReadId(IMMDevice* device) {
    LPWSTR id = nullptr;
    Require(SUCCEEDED(device->GetId(&id)) && id, "GetId failed");
    std::wstring value(id);
    CoTaskMemFree(id);
    return value;
}

qar::ConfigV1 MakeConfig(const wchar_t* capture, const wchar_t* render) {
    qar::ConfigV1 config = {};
    config.magic = qar::kConfigMagic;
    config.size = sizeof(config);
    config.version = qar::kConfigVersion;
    config.parentPid = 10;
    config.childPid = 11;
    config.hostGeneration = 77;
    wcscpy_s(config.captureEndpointId, capture);
    wcscpy_s(config.renderEndpointId, render);
    return config;
}

void TestCfgMetadataAndGeneration() {
    const uint32_t stride4[] = {0x12345, 0x23456};
    Require(qar::FindCfgEntry(reinterpret_cast<const uint8_t*>(stride4), 2, 4, 0x12345, false) ==
        qar::CfgEntryResult::valid, "stride4 exact RVA not found");
    Require(qar::FindCfgEntry(reinterpret_cast<const uint8_t*>(stride4), 2, 4, 0x12346, false) ==
        qar::CfgEntryResult::missing, "stride4 misaligned RVA matched");

    const uint8_t stride5[] = {
        0x45, 0x23, 0x01, 0x00, 0x00,
        0x56, 0x34, 0x02, 0x00, qar::kCfgExportSuppressed,
        0x67, 0x45, 0x03, 0x00, qar::kCfgFidSuppressed,
    };
    Require(qar::FindCfgEntry(stride5, 3, 5, 0x12345, false) == qar::CfgEntryResult::valid,
        "stride5 RVA bytes were altered");
    Require(qar::FindCfgEntry(stride5, 3, 5, 0x23456, false) == qar::CfgEntryResult::suppressed,
        "export-suppressed target accepted without GetProcAddress");
    Require(qar::FindCfgEntry(stride5, 3, 5, 0x23456, true) == qar::CfgEntryResult::valid,
        "GetProcAddress export-suppressed target rejected");
    Require(qar::FindCfgEntry(stride5, 3, 5, 0x34567, true) == qar::CfgEntryResult::suppressed,
        "FID-suppressed target accepted");
    uint8_t unknownFlag[5] = {0x45, 0x23, 0x01, 0x00, 0x80};
    Require(qar::FindCfgEntry(unknownFlag, 1, 5, 0x12345, false) == qar::CfgEntryResult::unsupported_metadata,
        "unknown GFIDS metadata accepted");
    Require(qar::FindCfgEntry(stride5, 3, 6, 0x12345, false) == qar::CfgEntryResult::malformed,
        "unsupported GFIDS stride accepted");

    uint64_t parsed = 99;
    Require(ParseSafeGeneration(true, 9007199254740991.0, &parsed) &&
        parsed == 9007199254740991ull, "max safe generation rejected");
    Require(ParseSafeGeneration(true, 1.0, &parsed) && parsed == 1, "positive generation rejected");
    const double invalid[] = {0.0, -1.0, 1.5, std::numeric_limits<double>::infinity(),
        std::numeric_limits<double>::quiet_NaN(), 9007199254740992.0};
    for (double value : invalid) {
        parsed = 99;
        Require(!ParseSafeGeneration(true, value, &parsed) && parsed == 0,
            "invalid host generation accepted");
    }
    Require(!ParseSafeGeneration(false, 1.0, &parsed), "missing host generation accepted");
    Require(!qar::DenyReentrantEnumeratorActivation(false, true),
        "outer enumerator activation incorrectly treated as reentry");
    Require(qar::DenyReentrantEnumeratorActivation(true, true),
        "reentrant enumerator activation was not denied");
    Require(!qar::DenyReentrantEnumeratorActivation(true, false),
        "unrelated reentrant COM activation was denied by policy");
}

void TestPolicyRepeatAndLaunchClassification() {
    qar::ParentOptions first = {};
    first.qqImagePath = L"C:\\QQ\\QQ.exe";
    first.guardDllPath = L"C:\\QQ\\QQCallAudioRouteGuard-v1.dll";
    first.qqBuild = L"9.9.26-44343";
    first.captureEndpointId = L"opaque-capture-id";
    first.renderEndpointId = L"opaque-render-id";
    first.hostGeneration = 8;
    qar::ParentOptions repeated = first;
    repeated.qqImagePath = L"c:\\qq\\qq.EXE";
    repeated.guardDllPath = L"c:\\qq\\QQCallAudioRouteGuard-v1.DLL";
    Require(qar::SameParentPolicy(first, repeated), "exact policy repeat rejected");
    repeated.hostGeneration++;
    Require(!qar::SameParentPolicy(first, repeated), "generation policy change accepted");
    repeated = first;
    repeated.captureEndpointId = L"OPAQUE-capture-id";
    Require(!qar::SameParentPolicy(first, repeated), "opaque endpoint case change accepted");
    repeated = first;
    repeated.qqBuild = L"different-build";
    Require(!qar::SameParentPolicy(first, repeated), "build change accepted");
    repeated = first;
    repeated.guardDllPath = L"C:\\QQ\\different.dll";
    Require(!qar::SameParentPolicy(first, repeated), "DLL change accepted");
    repeated = first;
    repeated.qqImagePath = L"C:\\QQ\\other.exe";
    Require(!qar::SameParentPolicy(first, repeated), "QQ image change accepted");
    repeated = first;
    repeated.renderEndpointId = L"different-render-id";
    Require(!qar::SameParentPolicy(first, repeated), "render endpoint change accepted");

    using qar::LaunchClassification;
    Require(qar::ClassifyLaunchPath(L"C:\\QQ\\QQ.exe", L"C:\\QQ\\QQ.exe", true, true, true) ==
        LaunchClassification::target_audio_service, "exact AudioService launch not targeted");
    Require(qar::ClassifyLaunchPath(L"C:\\tools\\helper.exe", L"C:\\QQ\\QQ.exe", false, true, true) ==
        LaunchClassification::unrelated, "unrelated control child classified as target");
    Require(qar::ClassifyLaunchPath(L"D:\\other\\QQ.exe", L"C:\\QQ\\QQ.exe", true, true, true) ==
        LaunchClassification::unrelated, "different same-named executable blocked");
    Require(qar::ClassifyLaunchPath(L"", L"C:\\QQ\\QQ.exe", true, false, true) ==
        LaunchClassification::reject_ambiguous_candidate, "unresolved target candidate allowed");
    Require(qar::ClassifyLaunchPath(L"C:\\QQ\\QQ.exe", L"C:\\QQ\\QQ.exe", true, true, false) ==
        LaunchClassification::reject_ambiguous_candidate, "malformed target command allowed");
    Require(qar::CommandPrefixNamesConfiguredImage(
        L"C:\\Program Files\\QQ\\QQ.exe --type=utility --utility-sub-type=audio.mojom.AudioService",
        L"C:\\Program Files\\QQ\\QQ.exe"), "unquoted target executable prefix was not recognized");
    Require(!qar::CommandPrefixNamesConfiguredImage(
        L"C:\\Program Files\\QQ\\QQ.exe.helper --type=utility --utility-sub-type=audio.mojom.AudioService",
        L"C:\\Program Files\\QQ\\QQ.exe"), "lookalike executable suffix was treated as target");
    Require(!qar::ExistingChildMustBlockInstall(true, true, false),
        "inspectable unrelated direct child blocked guard installation");
    Require(qar::ExistingChildMustBlockInstall(true, true, true),
        "existing configured-image child did not block installation");
    Require(qar::ExistingChildMustBlockInstall(false, false, false) &&
        qar::ExistingChildMustBlockInstall(true, false, false),
        "uninspectable existing child did not block installation");
}

void TestReadyBinding() {
    qar::ConfigV1 config = MakeConfig(L"capture", L"render");
    config.childPid = 1234;
    config.childCreationTime = {0x12345678, 0x9abcdef0};
    config.hostGeneration = 991;
    qar::StatusV1 status = {};
    status.magic = qar::kConfigMagic;
    status.size = sizeof(status);
    status.version = qar::kStatusVersion;
    status.phase = 1;
    status.childPid = config.childPid;
    status.childCreationTime = config.childCreationTime;
    status.hostGeneration = config.hostGeneration;
    Require(qar::ValidateReadyStatus(status, config), "correct ready report rejected");
    status.version = qar::kConfigVersion;
    Require(!qar::ValidateReadyStatus(status, config), "legacy status layout accepted");
    status.version = 2;
    Require(!qar::ValidateReadyStatus(status, config), "version 2 status layout accepted");
    status.version = qar::kStatusVersion;
    status.childPid++;
    Require(!qar::ValidateReadyStatus(status, config), "ready report from another PID accepted");
    status.childPid = config.childPid;
    status.childCreationTime.dwLowDateTime++;
    Require(!qar::ValidateReadyStatus(status, config), "ready report with another FILETIME accepted");
    status.childCreationTime = config.childCreationTime;
    status.hostGeneration++;
    Require(!qar::ValidateReadyStatus(status, config), "stale generation ready report accepted");
    status.hostGeneration = config.hostGeneration;
    status.phase = 0;
    Require(!qar::ValidateReadyStatus(status, config), "pre-ready phase accepted");
    status.phase = 1;
    status.failureCode = static_cast<uint32_t>(qar::FailureCode::target_invalid);
    Require(!qar::ValidateReadyStatus(status, config), "failed child reported ready");
}

struct FakeBackend final : qar::HookBackend {
    enum class Failure { none, init, create, cfg, queue, apply } failure = Failure::none;
    size_t failIndex = 0;
    size_t cursor = 0;
    size_t rollbackCount = 0;
    size_t queued = 0;
    bool enabled = false;
    bool initialize() override { return failure != Failure::init; }
    bool create(qar::HookRequest* request) override {
        const size_t index = cursor++;
        if (failure == Failure::create && index == failIndex) return false;
        request->target = reinterpret_cast<void*>(index + 1);
        request->original = reinterpret_cast<void*>(index + 101);
        request->created = true;
        return true;
    }
    bool markControlFlowTargets(qar::HookRequest*) override {
        return !(failure == Failure::cfg && cursor - 1 == failIndex);
    }
    bool queueEnable(qar::HookRequest*) override {
        if (failure == Failure::queue && queued == failIndex) return false;
        ++queued;
        return true;
    }
    bool applyQueued() override {
        if (failure == Failure::apply) return false;
        enabled = true;
        return true;
    }
    void rollback(qar::HookRequest* requests, size_t count) override {
        rollbackCount = count;
        enabled = false;
        for (size_t i = 0; i < count; ++i) requests[i].created = false;
    }
};

void TestHookTransactions() {
    qar::HookRequest requests[3] = {};
    for (auto failure : {FakeBackend::Failure::none, FakeBackend::Failure::init,
                         FakeBackend::Failure::create, FakeBackend::Failure::cfg,
                         FakeBackend::Failure::queue, FakeBackend::Failure::apply}) {
        FakeBackend backend;
        backend.failure = failure;
        backend.failIndex = failure == FakeBackend::Failure::queue ? 1 : 2;
        const bool installed = qar::InstallHookSet(&backend, requests, 3);
        Require(installed == (failure == FakeBackend::Failure::none), "hook transaction outcome mismatch");
        if (installed) Require(backend.enabled && backend.rollbackCount == 0, "successful hook transaction rolled back");
        else if (failure != FakeBackend::Failure::init)
            Require(!backend.enabled && backend.rollbackCount > 0, "failed hook transaction left hooks enabled");
    }
}

enum class ReentryApi { cocreate, cocreateex, cogetclassobject };
struct ReentryMock {
    ReentryApi api = ReentryApi::cocreate;
    uint32_t originalCalls = 0;
    HRESULT nestedResult = S_OK;
    void* nestedOutput = reinterpret_cast<void*>(1);
    HRESULT nestedMultiResult = S_OK;
    void* nestedMultiOutput = reinterpret_cast<void*>(1);
};
ReentryMock* g_reentryMock = nullptr;

HRESULT WINAPI FakeReentrantCoCreate(REFCLSID clsid, IUnknown*, DWORD, REFIID, LPVOID* result) {
    ReentryMock* mock = g_reentryMock;
    if (!mock) return E_UNEXPECTED;
    ++mock->originalCalls;
    if (clsid == __uuidof(MMDeviceEnumerator)) {
        if (result) *result = reinterpret_cast<void*>(0x1111);
        return S_OK;
    }
    if (mock->api == ReentryApi::cocreate) {
        mock->nestedOutput = reinterpret_cast<void*>(1);
        mock->nestedResult = QarTestCallCoCreate(__uuidof(MMDeviceEnumerator), nullptr,
            CLSCTX_INPROC_SERVER, IID_IUnknown, &mock->nestedOutput);
    }
    if (result) *result = nullptr;
    return S_OK;
}

HRESULT WINAPI FakeReentrantCoCreateEx(REFCLSID clsid, IUnknown*, DWORD, COSERVERINFO*, DWORD count,
                                       MULTI_QI* results) {
    ReentryMock* mock = g_reentryMock;
    if (!mock) return E_UNEXPECTED;
    ++mock->originalCalls;
    if (clsid == __uuidof(MMDeviceEnumerator)) {
        if (results && count) {
            results[0].pItf = reinterpret_cast<IUnknown*>(0x2222);
            results[0].hr = S_OK;
        }
        return S_OK;
    }
    if (mock->api == ReentryApi::cocreateex) {
        MULTI_QI query = {};
        query.pIID = &IID_IUnknown;
        query.pItf = reinterpret_cast<IUnknown*>(1);
        mock->nestedMultiResult = QarTestCallCoCreateEx(__uuidof(MMDeviceEnumerator), nullptr,
            CLSCTX_INPROC_SERVER, nullptr, 1, &query);
        mock->nestedMultiOutput = query.pItf;
    }
    if (results && count) {
        results[0].pItf = nullptr;
        results[0].hr = S_OK;
    }
    return S_OK;
}

HRESULT WINAPI FakeReentrantCoGetClassObject(REFCLSID clsid, DWORD, COSERVERINFO*, REFIID, LPVOID* result) {
    ReentryMock* mock = g_reentryMock;
    if (!mock) return E_UNEXPECTED;
    ++mock->originalCalls;
    if (clsid == __uuidof(MMDeviceEnumerator)) {
        if (result) *result = reinterpret_cast<void*>(0x3333);
        return S_OK;
    }
    if (mock->api == ReentryApi::cogetclassobject) {
        mock->nestedOutput = reinterpret_cast<void*>(1);
        mock->nestedResult = QarTestCallCoGetClassObject(__uuidof(MMDeviceEnumerator),
            CLSCTX_INPROC_SERVER, nullptr, IID_IClassFactory, &mock->nestedOutput);
    }
    if (result) *result = nullptr;
    return S_OK;
}

void TestReentryAndUnsupportedActivation() {
    ReentryMock mock;
    g_reentryMock = &mock;
    QarTestSetCoCreateOriginal(reinterpret_cast<void*>(&FakeReentrantCoCreate));
    mock.api = ReentryApi::cocreate;
    void* outer = nullptr;
    Require(SUCCEEDED(QarTestCallCoCreate(GUID_NULL, nullptr, CLSCTX_INPROC_SERVER,
        IID_IUnknown, &outer)), "unrelated outer CoCreateInstance failed");
    Require(mock.originalCalls == 1 && mock.nestedResult == E_ACCESSDENIED && mock.nestedOutput == nullptr,
        "reentrant CoCreateInstance reached raw enumerator");

    mock = ReentryMock{};
    mock.api = ReentryApi::cocreateex;
    QarTestSetCoCreateExOriginal(reinterpret_cast<void*>(&FakeReentrantCoCreateEx));
    MULTI_QI outerQuery = {};
    outerQuery.pIID = &IID_IUnknown;
    Require(SUCCEEDED(QarTestCallCoCreateEx(GUID_NULL, nullptr, CLSCTX_INPROC_SERVER,
        nullptr, 1, &outerQuery)), "unrelated outer CoCreateInstanceEx failed");
    Require(mock.originalCalls == 1 && mock.nestedMultiResult == E_ACCESSDENIED &&
        mock.nestedMultiOutput == nullptr, "reentrant CoCreateInstanceEx reached raw enumerator");
    MULTI_QI invalidQuery = {};
    invalidQuery.pItf = reinterpret_cast<IUnknown*>(1);
    invalidQuery.hr = S_OK;
    Require(QarTestCallCoCreateEx(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_INPROC_SERVER,
        nullptr, 1, &invalidQuery) == E_INVALIDARG && invalidQuery.pItf == nullptr &&
        invalidQuery.hr == E_POINTER, "null CoCreateInstanceEx IID was not rejected and cleared");

    mock = ReentryMock{};
    mock.api = ReentryApi::cogetclassobject;
    QarTestSetCoGetClassObjectOriginal(reinterpret_cast<void*>(&FakeReentrantCoGetClassObject));
    outer = nullptr;
    Require(SUCCEEDED(QarTestCallCoGetClassObject(GUID_NULL, CLSCTX_INPROC_SERVER,
        nullptr, IID_IUnknown, &outer)), "unrelated outer CoGetClassObject failed");
    Require(mock.originalCalls == 1 && mock.nestedResult == E_ACCESSDENIED && mock.nestedOutput == nullptr,
        "reentrant CoGetClassObject reached raw enumerator");
    g_reentryMock = nullptr;

    auto* asyncOperation = reinterpret_cast<IActivateAudioInterfaceAsyncOperation*>(1);
    Require(QarTestCallActivateAudioInterfaceAsync(L"mock://unsupported", IID_IUnknown,
        nullptr, nullptr, nullptr) == E_POINTER,
        "ActivateAudioInterfaceAsync accepted a null operation output");
    Require(QarTestCallActivateAudioInterfaceAsync(L"mock://unsupported", IID_IUnknown,
        nullptr, nullptr, &asyncOperation) == E_ACCESSDENIED && asyncOperation == nullptr,
        "ActivateAudioInterfaceAsync was not denied and cleared");
    auto* dsound = reinterpret_cast<IDirectSound*>(1);
    Require(QarTestCallDirectSoundCreate(nullptr, &dsound, nullptr) == DSERR_ACCESSDENIED && dsound == nullptr,
        "DirectSound creation was not denied and cleared");
    auto* dsound8 = reinterpret_cast<IDirectSound8*>(1);
    Require(QarTestCallDirectSoundCreate8(nullptr, &dsound8, nullptr) == DSERR_ACCESSDENIED && !dsound8,
        "DirectSoundCreate8 was not denied and cleared");
    auto* dsoundCapture = reinterpret_cast<IDirectSoundCapture*>(1);
    Require(QarTestCallDirectSoundCaptureCreate(nullptr, &dsoundCapture, nullptr) == DSERR_ACCESSDENIED &&
        dsoundCapture == nullptr, "DirectSound capture was not denied and cleared");
    auto* dsoundCapture8 = reinterpret_cast<IDirectSoundCapture8*>(1);
    Require(QarTestCallDirectSoundCaptureCreate8(nullptr, &dsoundCapture8, nullptr) == DSERR_ACCESSDENIED &&
        !dsoundCapture8, "DirectSoundCaptureCreate8 was not denied and cleared");
    auto* duplex = reinterpret_cast<IDirectSoundFullDuplex*>(1);
    auto* duplexCapture = reinterpret_cast<IDirectSoundCaptureBuffer8*>(1);
    auto* duplexRender = reinterpret_cast<IDirectSoundBuffer8*>(1);
    Require(QarTestCallDirectSoundFullDuplexCreate(nullptr, nullptr, nullptr, nullptr, nullptr, 0,
        &duplex, &duplexCapture, &duplexRender, nullptr) == DSERR_ACCESSDENIED &&
        !duplex && !duplexCapture && !duplexRender, "DirectSound full duplex was not denied and cleared");
    HWAVEIN waveIn = reinterpret_cast<HWAVEIN>(1);
    Require(QarTestCallWaveInOpen(&waveIn, WAVE_MAPPER, nullptr, 0, 0, 0) == MMSYSERR_NOTENABLED && !waveIn,
        "waveInOpen was not denied and cleared");
    HWAVEOUT waveOut = reinterpret_cast<HWAVEOUT>(1);
    Require(QarTestCallWaveOutOpen(&waveOut, WAVE_MAPPER, nullptr, 0, 0, 0) == MMSYSERR_NOTENABLED && !waveOut,
        "waveOutOpen was not denied and cleared");
}

DWORD g_requestedContext = 0;
HRESULT WINAPI FakeLocalContext(REFCLSID, IUnknown*, DWORD context, REFIID, LPVOID* output) {
    g_requestedContext = context;
    if (output) *output = nullptr;
    return E_FAIL;
}
void TestLocalContext() {
    QarTestSetCoCreateOriginal(reinterpret_cast<void*>(&FakeLocalContext));
    void* output = reinterpret_cast<void*>(1);
    Require(QarTestCallCoCreate(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        __uuidof(IMMDeviceEnumerator), &output) == E_FAIL && !output &&
        g_requestedContext == CLSCTX_INPROC_SERVER, "CLSCTX_ALL did not resolve strictly locally");
    g_requestedContext = 0;
    Require(QarTestCallCoCreate(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_REMOTE_SERVER,
        __uuidof(IMMDeviceEnumerator), &output) == E_ACCESSDENIED && !output && !g_requestedContext,
        "remote-only enumerator request reached original factory");
}

struct BarrierMock {
    bool createOk = true;
    bool identityOk = true;
    bool injectOk = true;
    bool readyOk = true;
    bool resumeOk = true;
    bool replacedOnSecondIdentity = false;
    bool preactivatedClient = false;
    bool createdSuspended = false;
    bool childMainRan = false;
    uint32_t identityChecks = 0;
    uint32_t terminatedPid = 0;
    uint32_t ownedPid = 42;
    uint32_t unrelatedControlPid = 43;
    uint32_t terminateCount = 0;
    uint32_t closeCount = 0;
    uint32_t resumeCount = 0;
    std::vector<int> order;
};

bool BarrierCreate(void* context, qar::ProcessInformation* info, uint32_t* error) {
    auto* mock = static_cast<BarrierMock*>(context);
    mock->createdSuspended = true;
    mock->order.push_back(1);
    if (!mock->createOk) { *error = ERROR_ACCESS_DENIED; return false; }
    info->processHandle = 0x100;
    info->threadHandle = 0x200;
    info->identity = {mock->ownedPid, 0x100000001ull};
    return true;
}
bool BarrierIdentity(void* context, const qar::ProcessInformation*) {
    auto* mock = static_cast<BarrierMock*>(context);
    ++mock->identityChecks;
    return mock->identityOk && !(mock->replacedOnSecondIdentity && mock->identityChecks == 2);
}
bool BarrierInject(void* context, const qar::ProcessInformation*, uint32_t* error) {
    auto* mock = static_cast<BarrierMock*>(context);
    mock->order.push_back(2);
    if (mock->preactivatedClient || !mock->injectOk) { *error = ERROR_ACCESS_DENIED; return false; }
    return true;
}
bool BarrierReady(void* context, const qar::ProcessInformation*, uint32_t* error) {
    auto* mock = static_cast<BarrierMock*>(context);
    mock->order.push_back(3);
    if (mock->childMainRan) { *error = ERROR_INVALID_STATE; return false; }
    if (!mock->readyOk) { *error = ERROR_TIMEOUT; return false; }
    return true;
}
bool BarrierResume(void* context, const qar::ProcessInformation*, uint32_t* error) {
    auto* mock = static_cast<BarrierMock*>(context);
    mock->order.push_back(4);
    ++mock->resumeCount;
    if (!mock->resumeOk) { *error = ERROR_ACCESS_DENIED; return false; }
    mock->childMainRan = true;
    return true;
}
void BarrierTerminate(void* context, const qar::ProcessInformation* info) {
    auto* mock = static_cast<BarrierMock*>(context);
    ++mock->terminateCount;
    mock->terminatedPid = info->identity.pid;
}
void BarrierClose(void* context, const qar::ProcessInformation*) { ++static_cast<BarrierMock*>(context)->closeCount; }

qar::LaunchResult RunBarrierMock(BarrierMock* mock, bool callerSuspended, qar::ProcessInformation* output) {
    qar::LaunchCallbacks callbacks = {mock, BarrierCreate, BarrierIdentity, BarrierInject,
        BarrierReady, BarrierResume, BarrierTerminate, BarrierClose};
    return qar::RunLaunchBarrier(callbacks, callerSuspended, output);
}

void TestLaunchBarrier() {
    BarrierMock createFailure;
    createFailure.createOk = false;
    qar::ProcessInformation output = {0xfeed, 0xbeef, {999, 888}};
    auto result = RunBarrierMock(&createFailure, false, &output);
    Require(!result.success && result.processInformationCleared && output.processHandle == 0 &&
        createFailure.terminateCount == 0, "failed create did not clear output without terminating an unowned child");

    BarrierMock normal;
    output = {};
    result = RunBarrierMock(&normal, false, &output);
    Require(result.success && normal.createdSuspended && normal.childMainRan && normal.resumeCount == 1,
        "normal targeted child was not resumed after readiness");
    Require(normal.order == std::vector<int>({1, 2, 3, 4}), "barrier order did not enforce early readiness");
    Require(output.identity.pid == normal.ownedPid && output.identity.creationTime == 0x100000001ull,
        "barrier returned a different child identity");

    BarrierMock callerSuspended;
    result = RunBarrierMock(&callerSuspended, true, &output);
    Require(result.success && !result.resumedOwnedThread && callerSuspended.resumeCount == 0 &&
        !callerSuspended.childMainRan, "caller CREATE_SUSPENDED semantics were not preserved");

    BarrierMock unready;
    unready.readyOk = false;
    result = RunBarrierMock(&unready, false, &output);
    Require(!result.success && result.terminatedOwnedChild && result.processInformationCleared &&
        unready.terminatedPid == unready.ownedPid && unready.terminateCount == 1 && unready.closeCount == 1,
        "not-ready owned child was not terminated and cleared");
    Require(unready.terminatedPid != unready.unrelatedControlPid, "failure affected an unrelated control child");

    BarrierMock injectionFailure;
    injectionFailure.injectOk = false;
    result = RunBarrierMock(&injectionFailure, false, &output);
    Require(!result.success && result.failure == qar::LaunchFailure::bind_or_inject_failed &&
        injectionFailure.terminateCount == 1 && output.processHandle == 0,
        "injection failure did not terminate only the new child");

    BarrierMock replacement;
    replacement.replacedOnSecondIdentity = true;
    result = RunBarrierMock(&replacement, false, &output);
    Require(!result.success && result.failure == qar::LaunchFailure::replacement_detected &&
        replacement.terminateCount == 1 && output.processHandle == 0, "child replacement not rejected");

    BarrierMock earlyClient;
    earlyClient.preactivatedClient = true;
    result = RunBarrierMock(&earlyClient, false, &output);
    Require(!result.success && earlyClient.terminateCount == 1,
        "already activated client counterexample was accepted");

    BarrierMock resumeFailure;
    resumeFailure.resumeOk = false;
    result = RunBarrierMock(&resumeFailure, false, &output);
    Require(!result.success && result.failure == qar::LaunchFailure::resume_failed &&
        resumeFailure.terminateCount == 1 && output.threadHandle == 0, "resume failure leaked child");
}

void TestIdentityBinding() {
    qar::ConfigV1 config = MakeConfig(L"capture", L"render");
    config.parentPid = 11;
    config.childPid = 12;
    config.parentCreationTime = {3, 4};
    config.childCreationTime = {5, 6};
    qar::IdentityObservationV1 observed = {};
    observed.childPid = config.childPid;
    observed.parentPid = config.parentPid;
    observed.childCreationTime = config.childCreationTime;
    observed.parentCreationTime = config.parentCreationTime;
    observed.childActorMatches = observed.parentActorMatches = true;
    observed.parentImageMatches = observed.childImageMatches = observed.audioServiceCommandMatches = true;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::none,
        "valid process binding rejected");
    observed.childPid++;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::child_identity_mismatch,
        "wrong child PID accepted");
    observed.childPid = config.childPid;
    observed.childCreationTime.dwLowDateTime++;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::child_identity_mismatch,
        "wrong child FILETIME accepted");
    observed.childCreationTime = config.childCreationTime;
    observed.parentPid++;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::parent_identity_mismatch,
        "wrong parent PID accepted");
    observed.parentPid = config.parentPid;
    observed.parentCreationTime.dwHighDateTime++;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::parent_identity_mismatch,
        "wrong parent FILETIME accepted");
    observed.parentCreationTime = config.parentCreationTime;
    observed.childActorMatches = false;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::actor_mismatch,
        "wrong actor accepted");
    observed.childActorMatches = true;
    observed.childImageMatches = false;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::wrong_image_or_command,
        "wrong child image accepted");
    observed.childImageMatches = true;
    observed.audioServiceCommandMatches = false;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::wrong_image_or_command,
        "wrong utility command accepted");
    observed.audioServiceCommandMatches = true;
    observed.parentActorMatches = false;
    Require(qar::ValidateObservedIdentity(config, observed) == qar::FailureCode::actor_mismatch,
        "wrong parent actor accepted");
}

struct FakeWorld {
    EndpointStats physicalCapture, virtualCapture, physicalRender, virtualRender, inactiveCapture;
    FakeDevice* physicalCaptureDevice;
    FakeDevice* virtualCaptureDevice;
    FakeDevice* physicalRenderDevice;
    FakeDevice* virtualRenderDevice;
    FakeDevice* inactiveCaptureDevice;
    FakeEnumerator* enumerator;
    FakeWorld()
        : physicalCaptureDevice(new FakeDevice(L"mock://physical/mic-array", eCapture, &physicalCapture)),
          virtualCaptureDevice(new FakeDevice(L"mock://virtual/line-1/opaque#9f", eCapture, &virtualCapture)),
          physicalRenderDevice(new FakeDevice(L"mock://physical/realtek", eRender, &physicalRender)),
          virtualRenderDevice(new FakeDevice(L"mock://virtual/vmin-5/opaque#42", eRender, &virtualRender)),
          inactiveCaptureDevice(new FakeDevice(L"mock://virtual/inactive", eCapture, &inactiveCapture,
                                               DEVICE_STATE_DISABLED)),
          enumerator(new FakeEnumerator({physicalCaptureDevice, virtualCaptureDevice,
              physicalRenderDevice, virtualRenderDevice, inactiveCaptureDevice})) {}
    ~FakeWorld() {
        if (enumerator) enumerator->Release();
        physicalCaptureDevice->Release();
        virtualCaptureDevice->Release();
        physicalRenderDevice->Release();
        virtualRenderDevice->Release();
        inactiveCaptureDevice->Release();
    }
};

void ActivateAndRelease(IMMDevice* device) {
    void* activated = nullptr;
    Require(SUCCEEDED(device->Activate(__uuidof(IAudioClient), CLSCTX_INPROC_SERVER, nullptr, &activated)) && activated,
        "mock IAudioClient activation failed");
    IUnknown* client = static_cast<IUnknown*>(activated);
    IAudioClient2* client2 = nullptr;
    IAudioClient3* client3 = nullptr;
    Require(SUCCEEDED(client->QueryInterface(__uuidof(IAudioClient2), reinterpret_cast<void**>(&client2))) && client2,
        "mock IAudioClient2 query failed");
    Require(SUCCEEDED(client->QueryInterface(__uuidof(IAudioClient3), reinterpret_cast<void**>(&client3))) && client3,
        "mock IAudioClient3 query failed");
    client2->Release();
    client3->Release();
    client->Release();
}

class FakeNotificationClient final : public IMMNotificationClient {
public:
    ULONG refs = 1;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != IID_IUnknown && iid != __uuidof(IMMNotificationClient)) return E_NOINTERFACE;
        *out = static_cast<IMMNotificationClient*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { return --refs; }
    HRESULT STDMETHODCALLTYPE OnDeviceStateChanged(LPCWSTR, DWORD) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnDeviceAdded(LPCWSTR) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnDeviceRemoved(LPCWSTR) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnDefaultDeviceChanged(EDataFlow, ERole, LPCWSTR) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPropertyValueChanged(LPCWSTR, const PROPERTYKEY) override { return S_OK; }
};

void TestComRoutingAndIdentity() {
    FakeWorld world;
    const wchar_t captureId[] = L"mock://virtual/line-1/opaque#9f";
    const wchar_t renderId[] = L"mock://virtual/vmin-5/opaque#42";
    qar::ConfigV1 config = MakeConfig(captureId, renderId);
    IMMDeviceEnumerator* wrapped = nullptr;
    Require(SUCCEEDED(QarTestWrapEnumerator(world.enumerator, &config, &wrapped)) && wrapped,
        "fake enumerator wrapping failed");
    FakeNotificationClient client;
    IMMNotificationClient* notification = &client;
    Require(wrapped->RegisterEndpointNotificationCallback(nullptr) == E_POINTER,
        "null notification accepted");
    Require(wrapped->UnregisterEndpointNotificationCallback(nullptr) == E_POINTER,
        "null notification unregister accepted");
    Require(wrapped->RegisterEndpointNotificationCallback(notification) == S_OK &&
        world.enumerator->notificationClient == notification, "notification registration not forwarded");
    Require(wrapped->UnregisterEndpointNotificationCallback(notification) == S_OK &&
        world.enumerator->notificationClient == nullptr, "notification unregister not forwarded");
    world.enumerator->notificationResult = E_OUTOFMEMORY;
    Require(wrapped->RegisterEndpointNotificationCallback(notification) == E_OUTOFMEMORY,
        "notification registration failure hidden");
    Require(wrapped->UnregisterEndpointNotificationCallback(notification) == E_OUTOFMEMORY,
        "notification unregister failure hidden");
    world.enumerator->notificationResult = S_OK;
    Require(client.refs == 1, "notification forwarding changed caller-owned lifetime");
    world.enumerator->Release();
    world.enumerator = nullptr;

    const ERole roles[] = {eConsole, eMultimedia, eCommunications};
    for (ERole role : roles) {
        IMMDevice* endpoint = nullptr;
        Require(SUCCEEDED(wrapped->GetDefaultAudioEndpoint(eCapture, role, &endpoint)) && endpoint,
            "capture role default lookup failed");
        Require(ReadId(endpoint) == captureId, "capture role did not route to opaque Line1 target");
        ActivateAndRelease(endpoint);
        endpoint->Release();
        endpoint = nullptr;
        Require(SUCCEEDED(wrapped->GetDefaultAudioEndpoint(eRender, role, &endpoint)) && endpoint,
            "render role default lookup failed");
        Require(ReadId(endpoint) == renderId, "render role did not route to opaque VMIn5 target");
        ActivateAndRelease(endpoint);
        endpoint->Release();
    }
    IMMDevice* invalidOutput = reinterpret_cast<IMMDevice*>(1);
    Require(wrapped->GetDefaultAudioEndpoint(eAll, eConsole, &invalidOutput) == E_INVALIDARG &&
        invalidOutput == nullptr, "invalid default-flow call did not clear its output");
    invalidOutput = reinterpret_cast<IMMDevice*>(1);
    Require(wrapped->GetDevice(L"mock://missing/endpoint", &invalidOutput) == E_NOTFOUND &&
        invalidOutput == nullptr, "missing explicit target did not fail with a null output");

    IMMDevice* explicitPhysical = nullptr;
    Require(SUCCEEDED(wrapped->GetDevice(L"mock://physical/mic-array", &explicitPhysical)) && explicitPhysical,
        "explicit physical metadata lookup failed");
    Require(ReadId(explicitPhysical) == L"mock://physical/mic-array", "explicit metadata ID was not preserved");
    IMMEndpoint* endpointView = nullptr;
    Require(SUCCEEDED(explicitPhysical->QueryInterface(__uuidof(IMMEndpoint), reinterpret_cast<void**>(&endpointView))) && endpointView,
        "wrapped IMMEndpoint query failed");
    void* unknownA = nullptr;
    void* unknownB = nullptr;
    Require(SUCCEEDED(explicitPhysical->QueryInterface(IID_IUnknown, &unknownA)), "IUnknown from IMMDevice failed");
    Require(SUCCEEDED(endpointView->QueryInterface(IID_IUnknown, &unknownB)), "IUnknown from IMMEndpoint failed");
    Require(unknownA == unknownB, "wrapped COM identity is unstable across interfaces");
    static_cast<IUnknown*>(unknownA)->Release();
    static_cast<IUnknown*>(unknownB)->Release();
    endpointView->Release();
    void* rawEscape = reinterpret_cast<void*>(1);
    Require(explicitPhysical->QueryInterface(__uuidof(IClassFactory), &rawEscape) == E_NOINTERFACE && !rawEscape,
        "unknown QueryInterface escaped to raw endpoint");
    ActivateAndRelease(explicitPhysical);
    explicitPhysical->Release();

    IMMDevice* explicitRender = nullptr;
    Require(SUCCEEDED(wrapped->GetDevice(L"mock://physical/realtek", &explicitRender)) && explicitRender,
        "explicit render metadata lookup failed");
    Require(ReadId(explicitRender) == L"mock://physical/realtek", "render metadata ID was not preserved");
    ActivateAndRelease(explicitRender);
    explicitRender->Release();

    IMMDeviceCollection* collection = nullptr;
    Require(SUCCEEDED(wrapped->EnumAudioEndpoints(eAll, DEVICE_STATE_ACTIVE, &collection)) && collection,
        "fake endpoint enumeration failed");
    UINT count = 0;
    Require(SUCCEEDED(collection->GetCount(&count)) && count == 4, "fake active endpoint count mismatch");
    IMMDevice* cached = nullptr;
    for (UINT index = 0; index < count; ++index) {
        IMMDevice* item = nullptr;
        Require(SUCCEEDED(collection->Item(index, &item)) && item, "wrapped collection item failed");
        if (ReadId(item) == L"mock://physical/realtek") cached = item;
        else {
            ActivateAndRelease(item);
            item->Release();
        }
    }
    collection->Release();
    Require(cached, "cached endpoint path not retained");
    ActivateAndRelease(cached);
    cached->Release();
    Require(world.physicalCapture.activationCount == 0 && world.physicalRender.activationCount == 0,
        "source physical endpoint was activated");
    Require(world.virtualCapture.activationCount > 0 && world.virtualRender.activationCount > 0,
        "configured fake virtual endpoints did not receive Activate");

    IMMDevice* propertySource = nullptr;
    Require(SUCCEEDED(wrapped->GetDevice(L"mock://physical/realtek", &propertySource)) && propertySource,
        "property source lookup failed");
    IPropertyStore* propertyStore = nullptr;
    Require(SUCCEEDED(propertySource->OpenPropertyStore(STGM_READ, &propertyStore)) && propertyStore,
        "read-only property store wrapper failed");
    void* storeUnknownA = nullptr;
    void* storeUnknownB = nullptr;
    Require(SUCCEEDED(propertyStore->QueryInterface(IID_IUnknown, &storeUnknownA)), "property-store identity lookup failed");
    Require(SUCCEEDED(propertyStore->QueryInterface(IID_IUnknown, &storeUnknownB)) && storeUnknownA == storeUnknownB,
        "property-store identity changed");
    static_cast<IUnknown*>(storeUnknownA)->Release();
    static_cast<IUnknown*>(storeUnknownB)->Release();
    PROPERTYKEY key = {};
    PROPVARIANT value;
    PropVariantInit(&value);
    g_propertyMode = PropertyMode::ui4;
    Require(SUCCEEDED(propertyStore->GetValue(key, &value)) && value.vt == VT_UI4 && value.ulVal == 17,
        "safe scalar property was not preserved");
    PropVariantClear(&value);
    PropVariantInit(&value);
    g_propertyMode = PropertyMode::vectorUi4;
    Require(SUCCEEDED(propertyStore->GetValue(key, &value)) && value.vt == (VT_VECTOR | VT_UI4) &&
        value.caul.cElems == 2 && value.caul.pElems[1] == 23, "safe data-only vector was not preserved");
    PropVariantClear(&value);
    PropVariantInit(&value);
    g_propertyMode = PropertyMode::vectorLpwstr;
    Require(SUCCEEDED(propertyStore->GetValue(key, &value)) && value.vt == (VT_VECTOR | VT_LPWSTR) &&
        value.calpwstr.cElems == 2 && wcscmp(value.calpwstr.pElems[1], L"metadata") == 0,
        "safe data-only string vector was not preserved");
    PropVariantClear(&value);
    for (PropertyMode mode : {PropertyMode::blob, PropertyMode::emptyBlob,
                              PropertyMode::oversizedBlob, PropertyMode::nullBlob}) {
        PropVariantInit(&value);
        g_propertyMode = mode;
        const HRESULT hr = propertyStore->GetValue(key, &value);
        if (mode == PropertyMode::blob || mode == PropertyMode::emptyBlob) {
            Require(SUCCEEDED(hr) && value.vt == VT_BLOB &&
                (value.blob.cbSize == 0 || value.blob.pBlobData[0] == 0x17), "bounded format blob changed");
        } else Require(hr == DISP_E_TYPEMISMATCH && value.vt == VT_EMPTY, "invalid format blob accepted");
        PropVariantClear(&value);
    }
    for (PropertyMode mode : {PropertyMode::lpwstr, PropertyMode::unknown,
                              PropertyMode::byrefUnknown, PropertyMode::arrayUnknown}) {
        PropVariantInit(&value);
        g_propertyMode = mode;
        const HRESULT hr = propertyStore->GetValue(key, &value);
        if (mode == PropertyMode::lpwstr) {
            Require(SUCCEEDED(hr) && value.vt == VT_LPWSTR &&
                wcscmp(value.pwszVal, L"mock endpoint metadata") == 0,
                "safe string property was not preserved");
        } else {
            Require(hr == DISP_E_TYPEMISMATCH && value.vt == VT_EMPTY,
                "interface/byref/nested property value escaped");
        }
        PropVariantClear(&value);
    }
    Require(propertyStore->SetValue(key, value) == STG_E_ACCESSDENIED &&
        propertyStore->Commit() == STG_E_ACCESSDENIED, "property-store writes were not denied");
    propertyStore->Release();
    propertySource->Release();

    IMMDevice* multiApartment = nullptr;
    Require(SUCCEEDED(wrapped->GetDevice(L"mock://physical/mic-array", &multiApartment)) && multiApartment,
        "multi-apartment source lookup failed");
    std::atomic<bool> crossApartmentOk{true};
    std::vector<std::thread> threads;
    for (int i = 0; i < 6; ++i) {
        threads.emplace_back([&]() {
            const HRESULT init = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
            if (FAILED(init)) { crossApartmentOk = false; return; }
            for (int j = 0; j < 20; ++j) {
                LPWSTR id = nullptr;
                if (FAILED(multiApartment->GetId(&id)) || !id || wcscmp(id, L"mock://physical/mic-array") != 0)
                    crossApartmentOk = false;
                if (id) CoTaskMemFree(id);
                void* activated = nullptr;
                if (FAILED(multiApartment->Activate(__uuidof(IAudioClient), CLSCTX_INPROC_SERVER, nullptr, &activated)) ||
                    !activated) crossApartmentOk = false;
                if (activated) static_cast<IUnknown*>(activated)->Release();
            }
            CoUninitialize();
        });
    }
    for (auto& thread : threads) thread.join();
    Require(crossApartmentOk, "free-threaded/GIT wrapper failed under apartment concurrency");
    multiApartment->Release();
    wrapped->Release();

    FakeWorld wrongFlowWorld;
    qar::ConfigV1 wrongFlowConfig = MakeConfig(L"mock://virtual/vmin-5/opaque#42", renderId);
    IMMDeviceEnumerator* wrongFlow = nullptr;
    Require(SUCCEEDED(QarTestWrapEnumerator(wrongFlowWorld.enumerator, &wrongFlowConfig, &wrongFlow)) &&
        wrongFlow, "wrong-flow fake setup failed");
    wrongFlowWorld.enumerator->Release();
    wrongFlowWorld.enumerator = nullptr;
    IMMDevice* rejected = nullptr;
    Require(wrongFlow->GetDefaultAudioEndpoint(eCapture, eConsole, &rejected) == E_ACCESSDENIED && !rejected,
        "capture selected a render-flow target");
    IMMDevice* wrongFlowSource = nullptr;
    Require(SUCCEEDED(wrongFlow->GetDevice(L"mock://physical/mic-array", &wrongFlowSource)) && wrongFlowSource,
        "wrong-flow explicit source metadata lookup failed");
    void* wrongFlowActivation = reinterpret_cast<void*>(1);
    Require(wrongFlowSource->Activate(__uuidof(IAudioClient), CLSCTX_INPROC_SERVER, nullptr,
        &wrongFlowActivation) == E_ACCESSDENIED && wrongFlowActivation == nullptr,
        "explicit metadata activation escaped a wrong-flow target");
    wrongFlowSource->Release();
    wrongFlow->Release();

    FakeDevice* inactive = new FakeDevice(L"mock://virtual/inactive", eCapture, nullptr, DEVICE_STATE_DISABLED);
    FakeEnumerator* inactiveEnumerator = new FakeEnumerator({inactive});
    qar::ConfigV1 inactiveConfig = MakeConfig(L"mock://virtual/inactive", L"mock://virtual/missing-render");
    IMMDeviceEnumerator* inactiveWrapper = nullptr;
    Require(SUCCEEDED(QarTestWrapEnumerator(inactiveEnumerator, &inactiveConfig, &inactiveWrapper)) && inactiveWrapper,
        "inactive target fake setup failed");
    inactiveEnumerator->Release();
    inactive->Release();
    rejected = nullptr;
    Require(inactiveWrapper->GetDefaultAudioEndpoint(eCapture, eConsole, &rejected) ==
        AUDCLNT_E_DEVICE_INVALIDATED && !rejected, "inactive endpoint was activated/fell back");
    rejected = reinterpret_cast<IMMDevice*>(1);
    Require(inactiveWrapper->GetDefaultAudioEndpoint(eRender, eConsole, &rejected) == E_NOTFOUND && !rejected,
        "missing configured render target fell back to a physical endpoint");
    inactiveWrapper->Release();
}

} // namespace

int wmain() {
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) return 2;
    try {
        Require(qar::VerifyCurrentProcessCfg(), "mock harness process does not have CFG enabled");
        bool mediaCandidate = false;
        const wchar_t* ppapi[] = {L"QQ.exe", L"--type=ppapi"};
        Require(qar::ClassifyMediaArguments(ppapi, 2, &mediaCandidate) && mediaCandidate, "PPAPI media child omitted");
        const wchar_t* audio[] = {L"QQ.exe", L"--type=utility", L"--utility-sub-type=audio.mojom.AudioService"};
        Require(qar::ClassifyMediaArguments(audio, 3, &mediaCandidate) && mediaCandidate, "AudioService child omitted");
        const wchar_t* mixed[] = {L"QQ.exe", L"--type=ppapi", L"--utility-sub-type=audio.mojom.AudioService"};
        Require(!qar::ClassifyMediaArguments(mixed, 3, &mediaCandidate) && mediaCandidate, "conflicting media child accepted");
        const wchar_t* renderer[] = {L"QQ.exe", L"--type=renderer"};
        Require(!qar::ClassifyMediaArguments(renderer, 2, &mediaCandidate) && !mediaCandidate, "renderer classified as guarded media child");
        Require(qar::IsColdLoaderModule(L"KernelBase.dll") && qar::IsColdLoaderModule(L"QQ.exe"), "cold loader baseline rejected");
        for (const auto* name : {L"QQNT.dll", L"AVSDKPlugin.dll", L"MMDevAPI.dll", L"AudioSes.dll", L"dsound.dll", L"unknown.dll"})
            Require(!qar::IsColdLoaderModule(name), "cold loader accepted an application/audio module");
        const unsigned char validLeaf[] = {0x33, 0xc0, 0xc3};
        const unsigned char invalidLeaf[] = {0xe9, 0xc0, 0xc3};
        Require(qar::IsOwnedLoaderLeaf(validLeaf) && !qar::IsOwnedLoaderLeaf(invalidLeaf) && !qar::IsOwnedLoaderLeaf(nullptr), "compiler leaf check failed");
        Require(qar::CanReleaseRemoteThreadBuffer(WAIT_OBJECT_0) && !qar::CanReleaseRemoteThreadBuffer(WAIT_TIMEOUT) && !qar::CanReleaseRemoteThreadBuffer(WAIT_FAILED), "live remote thread buffer would be freed");
        TestCfgMetadataAndGeneration();
        TestPolicyRepeatAndLaunchClassification();
        TestReadyBinding();
        TestHookTransactions();
        TestReentryAndUnsupportedActivation();
        TestLocalContext();
        TestLaunchBarrier();
        TestIdentityBinding();
        TestComRoutingAndIdentity();
        std::printf("audio_route_mock_tests=passed evidence=mock_only cfg_runtime=%s\n",
            qar::VerifyCurrentProcessCfg() ? "enabled" : "disabled");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "audio_route_mock_tests=failed code=%s\n", error.what());
        CoUninitialize();
        return 1;
    }
    CoUninitialize();
    return 0;
}
