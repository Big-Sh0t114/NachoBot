#include "audio_route_guard.h"
#include "runtime_internal.h"

#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <propidl.h>
#include <propsys.h>
#include <oleauto.h>

#include <atomic>
#include <cstring>
#include <cwchar>
#include <new>
#include <string>

namespace qar {
namespace {

IGlobalInterfaceTable* g_git = nullptr;
SRWLOCK g_gitLock = SRWLOCK_INIT;
std::atomic<uint64_t> g_rejectedActivationCount(0);
const IID kIidImmEndpoint = __uuidof(IMMEndpoint);

template <typename T>
class AutoCom final {
public:
    AutoCom() : value_(nullptr) {}
    explicit AutoCom(T* value) : value_(value) {}
    ~AutoCom() { reset(); }
    AutoCom(const AutoCom&) = delete;
    AutoCom& operator=(const AutoCom&) = delete;
    T* get() const { return value_; }
    T** put() { reset(); return &value_; }
    T* detach() { T* out = value_; value_ = nullptr; return out; }
    void reset(T* value = nullptr) {
        if (value_) value_->Release();
        value_ = value;
    }
private:
    T* value_;
};

class Policy final {
public:
    explicit Policy(const ConfigV1* config)
        : refs_(1), captureId_(config ? config->captureEndpointId : L""),
          renderId_(config ? config->renderEndpointId : L"") {}
    ULONG AddRef() { return ++refs_; }
    ULONG Release() {
        const ULONG refs = --refs_;
        if (refs == 0) delete this;
        return refs;
    }
    const std::wstring& target(EDataFlow flow) const {
        return flow == eCapture ? captureId_ : renderId_;
    }
private:
    std::atomic<ULONG> refs_;
    const std::wstring captureId_;
    const std::wstring renderId_;
};

template <typename T>
HRESULT RegisterGlobal(T* value, DWORD* cookie) {
    if (!value || !cookie || !g_git) return E_POINTER;
    return g_git->RegisterInterfaceInGlobal(value, __uuidof(T), cookie);
}

template <typename T>
HRESULT ResolveGlobal(DWORD cookie, T** value) {
    if (value) *value = nullptr;
    if (!value || !g_git || cookie == 0) return E_POINTER;
    return g_git->GetInterfaceFromGlobal(cookie, __uuidof(T), reinterpret_cast<void**>(value));
}

template <typename T>
void RevokeGlobal(DWORD* cookie) {
    if (g_git && cookie && *cookie) {
        g_git->RevokeInterfaceFromGlobal(*cookie);
        *cookie = 0;
    }
}

HRESULT CreateMarshaler(IUnknown* outer, IUnknown** marshaler) {
    if (marshaler) *marshaler = nullptr;
    if (!outer || !marshaler) return E_POINTER;
    return CoCreateFreeThreadedMarshaler(outer, marshaler);
}

HRESULT QueryMarshaler(IUnknown* marshaler, REFIID iid, void** out) {
    if (!marshaler || !out) return E_NOINTERFACE;
    return marshaler->QueryInterface(iid, out);
}

bool BoundedVariantWideString(LPCWSTR value, size_t* bytes = nullptr) {
    if (bytes) *bytes = 0;
    if (!value) return false;
    const wchar_t* end = static_cast<const wchar_t*>(wmemchr(value, L'\0', 32769));
    if (!end) return false;
    const size_t length = static_cast<size_t>(end - value);
    if (length > 32768) return false;
    if (bytes) *bytes = (length + 1) * sizeof(wchar_t);
    return true;
}

bool BoundedVariantAnsiString(LPCSTR value, size_t* bytes = nullptr) {
    if (bytes) *bytes = 0;
    if (!value) return false;
    const char* end = static_cast<const char*>(memchr(value, '\0', 32769));
    if (!end) return false;
    const size_t length = static_cast<size_t>(end - value);
    if (length > 32768) return false;
    if (bytes) *bytes = length + 1;
    return true;
}

template <typename Vector>
bool BoundedNumericVector(const Vector& value, size_t elementBytes) {
    return value.cElems <= 4096 && (value.cElems == 0 || value.pElems != nullptr) &&
        value.cElems <= (1024u * 1024u) / elementBytes;
}

bool SafeVariantVector(const PROPVARIANT* value) {
    const VARTYPE type = value->vt & VT_TYPEMASK;
    switch (type) {
    case VT_I1: return BoundedNumericVector(value->cac, sizeof(CHAR));
    case VT_UI1: return BoundedNumericVector(value->caub, sizeof(UCHAR));
    case VT_I2: return BoundedNumericVector(value->cai, sizeof(SHORT));
    case VT_UI2: return BoundedNumericVector(value->caui, sizeof(USHORT));
    case VT_I4: return BoundedNumericVector(value->cal, sizeof(LONG));
    case VT_UI4: return BoundedNumericVector(value->caul, sizeof(ULONG));
    case VT_I8: return BoundedNumericVector(value->cah, sizeof(LARGE_INTEGER));
    case VT_UI8: return BoundedNumericVector(value->cauh, sizeof(ULARGE_INTEGER));
    case VT_R4: return BoundedNumericVector(value->caflt, sizeof(FLOAT));
    case VT_R8: return BoundedNumericVector(value->cadbl, sizeof(DOUBLE));
    case VT_BOOL: return BoundedNumericVector(value->cabool, sizeof(VARIANT_BOOL));
    case VT_ERROR: return BoundedNumericVector(value->cascode, sizeof(SCODE));
    case VT_CY: return BoundedNumericVector(value->cacy, sizeof(CY));
    case VT_DATE: return BoundedNumericVector(value->cadate, sizeof(DATE));
    case VT_FILETIME: return BoundedNumericVector(value->cafiletime, sizeof(FILETIME));
    case VT_CLSID: return BoundedNumericVector(value->cauuid, sizeof(CLSID));
    case VT_LPSTR:
        if (!BoundedNumericVector(value->calpstr, sizeof(LPSTR))) return false;
        {
            size_t totalBytes = static_cast<size_t>(value->calpstr.cElems) * sizeof(LPSTR);
            for (ULONG i = 0; i < value->calpstr.cElems; ++i) {
                size_t itemBytes = 0;
                if (!BoundedVariantAnsiString(value->calpstr.pElems[i], &itemBytes) ||
                    itemBytes > 1024u * 1024u - totalBytes) return false;
                totalBytes += itemBytes;
            }
        }
        return true;
    case VT_LPWSTR:
        if (!BoundedNumericVector(value->calpwstr, sizeof(LPWSTR))) return false;
        {
            size_t totalBytes = static_cast<size_t>(value->calpwstr.cElems) * sizeof(LPWSTR);
            for (ULONG i = 0; i < value->calpwstr.cElems; ++i) {
                size_t itemBytes = 0;
                if (!BoundedVariantWideString(value->calpwstr.pElems[i], &itemBytes) ||
                    itemBytes > 1024u * 1024u - totalBytes) return false;
                totalBytes += itemBytes;
            }
        }
        return true;
    default: return false;
    }
}

bool SafeDataOnlyVariant(const PROPVARIANT* value) {
    if (!value || (value->vt & (VT_BYREF | VT_ARRAY | VT_RESERVED))) return false;
    if (value->vt & VT_VECTOR) return SafeVariantVector(value);
    switch (value->vt) {
    case VT_EMPTY:
    case VT_NULL:
    case VT_I1:
    case VT_UI1:
    case VT_I2:
    case VT_UI2:
    case VT_I4:
    case VT_UI4:
    case VT_INT:
    case VT_UINT:
    case VT_I8:
    case VT_UI8:
    case VT_R4:
    case VT_R8:
    case VT_BOOL:
    case VT_ERROR:
    case VT_CY:
    case VT_DATE:
    case VT_FILETIME:
    case VT_DECIMAL:
        return true;
    case VT_CLSID: return value->puuid != nullptr;
    case VT_BSTR: return !value->bstrVal || SysStringLen(value->bstrVal) <= 65536;
    // Windows publishes WAVEFORMATEX device format metadata as VT_BLOB.
    // This is bounded byte data, not an interface or an audio stream.
    case VT_BLOB: return value->blob.cbSize <= 1024u * 1024u &&
        (value->blob.cbSize == 0 || value->blob.pBlobData != nullptr);
    case VT_LPSTR: return BoundedVariantAnsiString(value->pszVal);
    case VT_LPWSTR: return BoundedVariantWideString(value->pwszVal);
    default: return false;
    }
}

void ClearAndInitializeVariant(PROPVARIANT* value) {
    if (!value) return;
    PropVariantClear(value);
    PropVariantInit(value);
}

HRESULT GetEndpointFlow(IMMDevice* device, EDataFlow* flow) {
    if (flow) *flow = eAll;
    if (!device || !flow) return E_POINTER;
    AutoCom<IMMEndpoint> endpoint;
    HRESULT hr = device->QueryInterface(IID_PPV_ARGS(endpoint.put()));
    if (FAILED(hr)) return hr;
    hr = endpoint.get()->GetDataFlow(flow);
    if (FAILED(hr)) return hr;
    return (*flow == eCapture || *flow == eRender) ? S_OK : E_ACCESSDENIED;
}

bool ValidRole(ERole role) {
    return role == eConsole || role == eMultimedia || role == eCommunications;
}

class DeviceWrapper;
class CollectionWrapper;
class PropertyStoreWrapper;

class EnumeratorWrapper final : public IMMDeviceEnumerator {
public:
    EnumeratorWrapper(DWORD cookie, Policy* policy)
        : refs_(1), enumCookie_(cookie), policy_(policy), marshaler_(nullptr), marshalHr_(E_FAIL) {
        policy_->AddRef();
        marshalHr_ = CreateMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~EnumeratorWrapper() {
        if (marshaler_) marshaler_->Release();
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie_);
        policy_->Release();
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (!refs) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE EnumAudioEndpoints(EDataFlow flow, DWORD stateMask,
                                                  IMMDeviceCollection** devices) override;
    HRESULT STDMETHODCALLTYPE GetDefaultAudioEndpoint(EDataFlow flow, ERole role,
                                                       IMMDevice** endpoint) override;
    HRESULT STDMETHODCALLTYPE GetDevice(LPCWSTR id, IMMDevice** device) override;
    HRESULT STDMETHODCALLTYPE RegisterEndpointNotificationCallback(IMMNotificationClient* client) override {
        if (!client) return E_POINTER;
        AutoCom<IMMDeviceEnumerator> raw;
        HRESULT hr = Raw(raw.put());
        if (SUCCEEDED(hr)) hr = raw.get()->RegisterEndpointNotificationCallback(client);
        RecordDiagnostic(DiagnosticStage::register_notification, hr);
        // The public callback carries device IDs and scalar metadata only. Its
        // lifetime remains the caller's responsibility, as required by MMDevice.
        return hr;
    }
    HRESULT STDMETHODCALLTYPE UnregisterEndpointNotificationCallback(IMMNotificationClient* client) override {
        if (!client) return E_POINTER;
        AutoCom<IMMDeviceEnumerator> raw;
        HRESULT hr = Raw(raw.put());
        if (SUCCEEDED(hr)) hr = raw.get()->UnregisterEndpointNotificationCallback(client);
        RecordDiagnostic(DiagnosticStage::unregister_notification, hr);
        return hr;
    }

    HRESULT Raw(IMMDeviceEnumerator** out) const { return ResolveGlobal(enumCookie_, out); }
    Policy* policy() const { return policy_; }
    HRESULT marshalHr() const { return marshalHr_; }
private:
    std::atomic<ULONG> refs_;
    DWORD enumCookie_;
    Policy* policy_;
    IUnknown* marshaler_;
    HRESULT marshalHr_;
};

class DeviceWrapper final : public IMMDevice, public IMMEndpoint {
public:
    DeviceWrapper(DWORD enumCookie, DWORD deviceCookie, Policy* policy, EDataFlow sourceFlow)
        : refs_(1), enumCookie_(enumCookie), deviceCookie_(deviceCookie), policy_(policy),
          sourceFlow_(sourceFlow), marshaler_(nullptr), marshalHr_(E_FAIL) {
        policy_->AddRef();
        marshalHr_ = CreateMarshaler(static_cast<IUnknown*>(static_cast<IMMDevice*>(this)), &marshaler_);
    }
    ~DeviceWrapper() {
        if (marshaler_) marshaler_->Release();
        RevokeGlobal<IMMDevice>(&deviceCookie_);
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie_);
        policy_->Release();
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (!refs) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE Activate(REFIID iid, DWORD clsctx, PROPVARIANT* params,
                                       void** out) override;
    HRESULT STDMETHODCALLTYPE OpenPropertyStore(DWORD access, IPropertyStore** store) override;
    HRESULT STDMETHODCALLTYPE GetId(LPWSTR* id) override;
    HRESULT STDMETHODCALLTYPE GetState(DWORD* state) override;
    HRESULT STDMETHODCALLTYPE GetDataFlow(EDataFlow* flow) override;
    HRESULT marshalHr() const { return marshalHr_; }
private:
    HRESULT ResolveDevice(IMMDevice** out) const { return ResolveGlobal(deviceCookie_, out); }
    HRESULT ResolveEnumerator(IMMDeviceEnumerator** out) const { return ResolveGlobal(enumCookie_, out); }
    HRESULT ResolveSelectedDevice(EDataFlow flow, IMMDevice** out) const;
    std::atomic<ULONG> refs_;
    DWORD enumCookie_;
    DWORD deviceCookie_;
    Policy* policy_;
    const EDataFlow sourceFlow_;
    IUnknown* marshaler_;
    HRESULT marshalHr_;
};

class CollectionWrapper final : public IMMDeviceCollection {
public:
    CollectionWrapper(DWORD enumCookie, DWORD collectionCookie, Policy* policy)
        : refs_(1), enumCookie_(enumCookie), collectionCookie_(collectionCookie), policy_(policy),
          marshaler_(nullptr), marshalHr_(E_FAIL) {
        policy_->AddRef();
        marshalHr_ = CreateMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~CollectionWrapper() {
        if (marshaler_) marshaler_->Release();
        RevokeGlobal<IMMDeviceCollection>(&collectionCookie_);
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie_);
        policy_->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (!refs) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE GetCount(UINT* count) override {
        if (count) *count = 0;
        if (!count) return E_POINTER;
        AutoCom<IMMDeviceCollection> raw;
        HRESULT hr = ResolveGlobal(collectionCookie_, raw.put());
        return FAILED(hr) ? hr : raw.get()->GetCount(count);
    }
    HRESULT STDMETHODCALLTYPE Item(UINT index, IMMDevice** device) override;
    HRESULT marshalHr() const { return marshalHr_; }
private:
    std::atomic<ULONG> refs_;
    DWORD enumCookie_;
    DWORD collectionCookie_;
    Policy* policy_;
    IUnknown* marshaler_;
    HRESULT marshalHr_;
};

class PropertyStoreWrapper final : public IPropertyStore {
public:
    explicit PropertyStoreWrapper(DWORD storeCookie)
        : refs_(1), storeCookie_(storeCookie), marshaler_(nullptr), marshalHr_(E_FAIL) {
        marshalHr_ = CreateMarshaler(static_cast<IUnknown*>(this), &marshaler_);
    }
    ~PropertyStoreWrapper() {
        if (marshaler_) marshaler_->Release();
        RevokeGlobal<IPropertyStore>(&storeCookie_);
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = --refs_;
        if (!refs) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE GetCount(DWORD* count) override;
    HRESULT STDMETHODCALLTYPE GetAt(DWORD index, PROPERTYKEY* key) override;
    HRESULT STDMETHODCALLTYPE GetValue(REFPROPERTYKEY key, PROPVARIANT* value) override;
    HRESULT STDMETHODCALLTYPE SetValue(REFPROPERTYKEY, REFPROPVARIANT) override { return STG_E_ACCESSDENIED; }
    HRESULT STDMETHODCALLTYPE Commit() override { return STG_E_ACCESSDENIED; }
    HRESULT marshalHr() const { return marshalHr_; }
private:
    std::atomic<ULONG> refs_;
    DWORD storeCookie_;
    IUnknown* marshaler_;
    HRESULT marshalHr_;
};

HRESULT WrapDevice(IMMDeviceEnumerator* rawEnum, IMMDevice* rawDevice, Policy* policy, IMMDevice** out) {
    if (out) *out = nullptr;
    if (!rawEnum || !rawDevice || !policy || !out) return E_POINTER;
    EDataFlow flow = eAll;
    HRESULT hr = GetEndpointFlow(rawDevice, &flow);
    if (FAILED(hr)) return hr;
    DWORD enumCookie = 0;
    DWORD deviceCookie = 0;
    hr = RegisterGlobal(rawEnum, &enumCookie);
    if (SUCCEEDED(hr)) hr = RegisterGlobal(rawDevice, &deviceCookie);
    if (FAILED(hr)) {
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie);
        RevokeGlobal<IMMDevice>(&deviceCookie);
        return hr;
    }
    DeviceWrapper* wrapper = new (std::nothrow) DeviceWrapper(enumCookie, deviceCookie, policy, flow);
    if (!wrapper) {
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie);
        RevokeGlobal<IMMDevice>(&deviceCookie);
        return E_OUTOFMEMORY;
    }
    if (FAILED(wrapper->marshalHr())) {
        hr = wrapper->marshalHr();
        delete wrapper;
        return hr;
    }
    *out = static_cast<IMMDevice*>(wrapper);
    return S_OK;
}

HRESULT WrapCollection(IMMDeviceEnumerator* rawEnum, IMMDeviceCollection* rawCollection,
                       Policy* policy, IMMDeviceCollection** out) {
    if (out) *out = nullptr;
    if (!rawEnum || !rawCollection || !policy || !out) return E_POINTER;
    DWORD enumCookie = 0;
    DWORD collectionCookie = 0;
    HRESULT hr = RegisterGlobal(rawEnum, &enumCookie);
    if (SUCCEEDED(hr)) hr = RegisterGlobal(rawCollection, &collectionCookie);
    if (FAILED(hr)) {
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie);
        RevokeGlobal<IMMDeviceCollection>(&collectionCookie);
        return hr;
    }
    CollectionWrapper* wrapper = new (std::nothrow) CollectionWrapper(enumCookie, collectionCookie, policy);
    if (!wrapper) {
        RevokeGlobal<IMMDeviceEnumerator>(&enumCookie);
        RevokeGlobal<IMMDeviceCollection>(&collectionCookie);
        return E_OUTOFMEMORY;
    }
    if (FAILED(wrapper->marshalHr())) {
        hr = wrapper->marshalHr();
        delete wrapper;
        return hr;
    }
    *out = wrapper;
    return S_OK;
}

HRESULT WrapStore(IPropertyStore* raw, IPropertyStore** out) {
    if (out) *out = nullptr;
    if (!raw || !out) return E_POINTER;
    DWORD cookie = 0;
    HRESULT hr = RegisterGlobal(raw, &cookie);
    if (FAILED(hr)) return hr;
    PropertyStoreWrapper* wrapper = new (std::nothrow) PropertyStoreWrapper(cookie);
    if (!wrapper) {
        RevokeGlobal<IPropertyStore>(&cookie);
        return E_OUTOFMEMORY;
    }
    if (FAILED(wrapper->marshalHr())) {
        hr = wrapper->marshalHr();
        delete wrapper;
        return hr;
    }
    *out = wrapper;
    return S_OK;
}

HRESULT QueryWrap(IUnknown* self, IUnknown* marshaler, REFIID iid, void** out,
                  const IID& supportedIid, void* supportedInterface,
                  const IID* secondIid = nullptr, void* secondInterface = nullptr) {
    if (out) *out = nullptr;
    if (!out) return E_POINTER;
    if (iid == IID_IUnknown || iid == supportedIid || (secondIid && iid == *secondIid)) {
        *out = iid == IID_IUnknown ? self : (iid == supportedIid ? supportedInterface : secondInterface);
        self->AddRef();
        return S_OK;
    }
    if (iid == IID_IMarshal) return QueryMarshaler(marshaler, iid, out);
    RecordDiagnostic(DiagnosticStage::unsupported_interface, E_NOINTERFACE);
    RecordInterfaceDiagnostic(iid, false);
    return E_NOINTERFACE;
}

HRESULT ValidateTarget(IMMDeviceEnumerator* rawEnum, const std::wstring& id,
                       EDataFlow requiredFlow, IMMDevice** out) {
    if (out) *out = nullptr;
    if (!rawEnum || !out || id.empty()) return E_INVALIDARG;
    AutoCom<IMMDevice> target;
    HRESULT hr = rawEnum->GetDevice(id.c_str(), target.put());
    if (FAILED(hr) || !target.get()) return FAILED(hr) ? hr : E_ACCESSDENIED;
    DWORD state = 0;
    hr = target.get()->GetState(&state);
    if (FAILED(hr)) return hr;
    if (state != DEVICE_STATE_ACTIVE) return AUDCLNT_E_DEVICE_INVALIDATED;
    EDataFlow targetFlow = eAll;
    hr = GetEndpointFlow(target.get(), &targetFlow);
    if (FAILED(hr)) return hr;
    if (targetFlow != requiredFlow) return E_ACCESSDENIED;
    LPWSTR returnedId = nullptr;
    hr = target.get()->GetId(&returnedId);
    if (FAILED(hr) || !returnedId) {
        if (returnedId) CoTaskMemFree(returnedId);
        return FAILED(hr) ? hr : E_ACCESSDENIED;
    }
    const bool exactId = id == returnedId;
    CoTaskMemFree(returnedId);
    if (!exactId) return E_ACCESSDENIED;
    *out = target.detach();
    return S_OK;
}

} // namespace

HRESULT InitializeGlobalInterfaceTable() {
    AcquireSRWLockShared(&g_gitLock);
    if (g_git) {
        ReleaseSRWLockShared(&g_gitLock);
        return S_OK;
    }
    ReleaseSRWLockShared(&g_gitLock);
    AcquireSRWLockExclusive(&g_gitLock);
    if (!g_git) {
        HRESULT hr = CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr, CLSCTX_INPROC_SERVER,
                                      IID_PPV_ARGS(&g_git));
        if (FAILED(hr)) {
            ReleaseSRWLockExclusive(&g_gitLock);
            return hr;
        }
    }
    ReleaseSRWLockExclusive(&g_gitLock);
    return S_OK;
}

HRESULT WrapEnumerator(IUnknown* raw, const ConfigV1* config, IMMDeviceEnumerator** wrapped) {
    if (wrapped) *wrapped = nullptr;
    if (!raw || !config || !wrapped) return E_POINTER;
    HRESULT hr = InitializeGlobalInterfaceTable();
    if (FAILED(hr)) return hr;
    AutoCom<IMMDeviceEnumerator> enumerator;
    hr = raw->QueryInterface(IID_PPV_ARGS(enumerator.put()));
    if (FAILED(hr)) return hr;
    Policy* policy = new (std::nothrow) Policy(config);
    if (!policy) return E_OUTOFMEMORY;
    DWORD cookie = 0;
    hr = RegisterGlobal(enumerator.get(), &cookie);
    if (FAILED(hr)) {
        policy->Release();
        return hr;
    }
    EnumeratorWrapper* wrapper = new (std::nothrow) EnumeratorWrapper(cookie, policy);
    policy->Release();
    if (!wrapper) {
        RevokeGlobal<IMMDeviceEnumerator>(&cookie);
        return E_OUTOFMEMORY;
    }
    if (FAILED(wrapper->marshalHr())) {
        hr = wrapper->marshalHr();
        delete wrapper;
        return hr;
    }
    *wrapped = wrapper;
    return S_OK;
}

HRESULT ValidateConfiguredEndpoints(IMMDeviceEnumerator* raw, const ConfigV1* config) {
    if (!raw || !config) return E_POINTER;
    if (!config->captureEndpointId[0] || !config->renderEndpointId[0]) return E_INVALIDARG;
    AutoCom<IMMDevice> capture;
    HRESULT hr = ValidateTarget(raw, config->captureEndpointId, eCapture, capture.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDevice> render;
    return ValidateTarget(raw, config->renderEndpointId, eRender, render.put());
}

HRESULT WrapRequestedEnumerator(REFIID iid, IUnknown* raw, const ConfigV1* config, void** out) {
    if (out) *out = nullptr;
    if (!out || !raw || !config) return E_POINTER;
    if (iid != IID_IUnknown && iid != __uuidof(IMMDeviceEnumerator)) return E_NOINTERFACE;
    AutoCom<IMMDeviceEnumerator> wrapper;
    HRESULT hr = WrapEnumerator(raw, config, wrapper.put());
    if (FAILED(hr)) return hr;
    hr = wrapper.get()->QueryInterface(iid, out);
    return hr;
}

HRESULT STDMETHODCALLTYPE EnumeratorWrapper::QueryInterface(REFIID iid, void** out) {
    return QueryWrap(static_cast<IUnknown*>(this), marshaler_, iid, out,
                     __uuidof(IMMDeviceEnumerator), static_cast<IMMDeviceEnumerator*>(this));
}

HRESULT STDMETHODCALLTYPE EnumeratorWrapper::EnumAudioEndpoints(EDataFlow flow, DWORD stateMask,
                                                               IMMDeviceCollection** devices) {
    RecordDiagnostic(DiagnosticStage::enumerate_endpoints, E_PENDING);
    if (devices) *devices = nullptr;
    if (!devices) return E_POINTER;
    AutoCom<IMMDeviceEnumerator> raw;
    HRESULT hr = Raw(raw.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDeviceCollection> collection;
    hr = raw.get()->EnumAudioEndpoints(flow, stateMask, collection.put());
    if (FAILED(hr) || !collection.get()) return FAILED(hr) ? hr : E_ACCESSDENIED;
    return WrapCollection(raw.get(), collection.get(), policy_, devices);
}

HRESULT STDMETHODCALLTYPE EnumeratorWrapper::GetDefaultAudioEndpoint(EDataFlow flow, ERole role,
                                                                      IMMDevice** endpoint) {
    RecordDiagnostic(DiagnosticStage::default_endpoint, E_PENDING);
    if (endpoint) *endpoint = nullptr;
    if (!endpoint) return E_POINTER;
    if (!ValidRole(role) || (flow != eCapture && flow != eRender)) return E_INVALIDARG;
    AutoCom<IMMDeviceEnumerator> raw;
    HRESULT hr = Raw(raw.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDevice> selected;
    hr = ValidateTarget(raw.get(), policy_->target(flow), flow, selected.put());
    if (FAILED(hr)) return hr;
    return WrapDevice(raw.get(), selected.get(), policy_, endpoint);
}

HRESULT STDMETHODCALLTYPE EnumeratorWrapper::GetDevice(LPCWSTR id, IMMDevice** device) {
    RecordDiagnostic(DiagnosticStage::get_device, E_PENDING);
    if (device) *device = nullptr;
    if (!device || !id) return E_POINTER;
    AutoCom<IMMDeviceEnumerator> raw;
    HRESULT hr = Raw(raw.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDevice> source;
    hr = raw.get()->GetDevice(id, source.put());
    if (FAILED(hr) || !source.get()) return FAILED(hr) ? hr : E_ACCESSDENIED;
    return WrapDevice(raw.get(), source.get(), policy_, device);
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::QueryInterface(REFIID iid, void** out) {
    return QueryWrap(static_cast<IUnknown*>(static_cast<IMMDevice*>(this)), marshaler_, iid, out,
                     __uuidof(IMMDevice), static_cast<IMMDevice*>(this),
                     &kIidImmEndpoint, static_cast<IMMEndpoint*>(this));
}

HRESULT DeviceWrapper::ResolveSelectedDevice(EDataFlow flow, IMMDevice** out) const {
    if (out) *out = nullptr;
    if (!out || (flow != eCapture && flow != eRender)) return E_INVALIDARG;
    AutoCom<IMMDeviceEnumerator> rawEnum;
    HRESULT hr = ResolveEnumerator(rawEnum.put());
    if (FAILED(hr)) return hr;
    return ValidateTarget(rawEnum.get(), policy_->target(flow), flow, out);
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::Activate(REFIID iid, DWORD clsctx, PROPVARIANT* params,
                                                  void** out) {
    RecordDiagnostic(DiagnosticStage::activate_device, E_PENDING);
    if (out) *out = nullptr;
    if (!out) return E_POINTER;
    AutoCom<IMMDevice> source;
    HRESULT hr = ResolveDevice(source.put());
    if (FAILED(hr)) return hr;
    const EDataFlow flow = sourceFlow_;
    RecordInterfaceDiagnostic(iid, true, static_cast<uint32_t>(flow));
    AutoCom<IMMDevice> selected;
    hr = ResolveSelectedDevice(flow, selected.put());
    if (FAILED(hr)) {
        ++g_rejectedActivationCount;
        return hr;
    }
    // Even a metadata object returned by explicit GetDevice/Item activates only
    // the configured endpoint of its immutable source flow.
    hr = selected.get()->Activate(iid, clsctx, params, out);
    RecordDiagnostic(DiagnosticStage::activate_device, hr);
    if (FAILED(hr) && *out) {
        static_cast<IUnknown*>(*out)->Release();
        *out = nullptr;
    } else if (SUCCEEDED(hr) && !*out) {
        return E_ACCESSDENIED;
    }
    return hr;
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::OpenPropertyStore(DWORD access, IPropertyStore** store) {
    if (store) *store = nullptr;
    if (!store) return E_POINTER;
    if (access != STGM_READ) return STG_E_ACCESSDENIED;
    AutoCom<IMMDevice> raw;
    HRESULT hr = ResolveDevice(raw.put());
    if (FAILED(hr)) return hr;
    AutoCom<IPropertyStore> propertyStore;
    hr = raw.get()->OpenPropertyStore(STGM_READ, propertyStore.put());
    if (FAILED(hr) || !propertyStore.get()) return FAILED(hr) ? hr : E_ACCESSDENIED;
    return WrapStore(propertyStore.get(), store);
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::GetId(LPWSTR* id) {
    if (id) *id = nullptr;
    if (!id) return E_POINTER;
    AutoCom<IMMDevice> raw;
    HRESULT hr = ResolveDevice(raw.put());
    if (FAILED(hr)) return hr;
    hr = raw.get()->GetId(id);
    if (FAILED(hr) && *id) {
        CoTaskMemFree(*id);
        *id = nullptr;
    }
    return hr;
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::GetState(DWORD* state) {
    if (state) *state = 0;
    if (!state) return E_POINTER;
    AutoCom<IMMDevice> raw;
    HRESULT hr = ResolveDevice(raw.put());
    return FAILED(hr) ? hr : raw.get()->GetState(state);
}

HRESULT STDMETHODCALLTYPE DeviceWrapper::GetDataFlow(EDataFlow* flow) {
    if (flow) *flow = eAll;
    if (!flow) return E_POINTER;
    *flow = sourceFlow_;
    return S_OK;
}

HRESULT STDMETHODCALLTYPE CollectionWrapper::QueryInterface(REFIID iid, void** out) {
    return QueryWrap(static_cast<IUnknown*>(this), marshaler_, iid, out,
                     __uuidof(IMMDeviceCollection), static_cast<IMMDeviceCollection*>(this));
}

HRESULT STDMETHODCALLTYPE CollectionWrapper::Item(UINT index, IMMDevice** device) {
    if (device) *device = nullptr;
    if (!device) return E_POINTER;
    AutoCom<IMMDeviceCollection> rawCollection;
    HRESULT hr = ResolveGlobal(collectionCookie_, rawCollection.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDeviceEnumerator> rawEnum;
    hr = ResolveGlobal(enumCookie_, rawEnum.put());
    if (FAILED(hr)) return hr;
    AutoCom<IMMDevice> source;
    hr = rawCollection.get()->Item(index, source.put());
    if (FAILED(hr) || !source.get()) return FAILED(hr) ? hr : E_ACCESSDENIED;
    return WrapDevice(rawEnum.get(), source.get(), policy_, device);
}

HRESULT STDMETHODCALLTYPE PropertyStoreWrapper::QueryInterface(REFIID iid, void** out) {
    return QueryWrap(static_cast<IUnknown*>(this), marshaler_, iid, out,
                     __uuidof(IPropertyStore), static_cast<IPropertyStore*>(this));
}

HRESULT STDMETHODCALLTYPE PropertyStoreWrapper::GetCount(DWORD* count) {
    if (count) *count = 0;
    if (!count) return E_POINTER;
    AutoCom<IPropertyStore> raw;
    HRESULT hr = ResolveGlobal(storeCookie_, raw.put());
    return FAILED(hr) ? hr : raw.get()->GetCount(count);
}

HRESULT STDMETHODCALLTYPE PropertyStoreWrapper::GetAt(DWORD index, PROPERTYKEY* key) {
    if (key) *key = PROPERTYKEY{};
    if (!key) return E_POINTER;
    AutoCom<IPropertyStore> raw;
    HRESULT hr = ResolveGlobal(storeCookie_, raw.put());
    return FAILED(hr) ? hr : raw.get()->GetAt(index, key);
}

HRESULT STDMETHODCALLTYPE PropertyStoreWrapper::GetValue(REFPROPERTYKEY key, PROPVARIANT* value) {
    if (!value) return E_POINTER;
    PropVariantInit(value);
    AutoCom<IPropertyStore> raw;
    HRESULT hr = ResolveGlobal(storeCookie_, raw.put());
    if (FAILED(hr)) return hr;
    hr = raw.get()->GetValue(key, value);
    if (FAILED(hr)) {
        RecordDiagnostic(DiagnosticStage::property_value, hr);
        ClearAndInitializeVariant(value);
        return hr;
    }
    if (!SafeDataOnlyVariant(value)) {
        RecordDiagnostic(DiagnosticStage::property_value, DISP_E_TYPEMISMATCH);
        ClearAndInitializeVariant(value);
        return DISP_E_TYPEMISMATCH;
    }
    RecordDiagnostic(DiagnosticStage::property_value, S_OK);
    return S_OK;
}

} // namespace qar

#if defined(QAR_TESTING)
extern "C" HRESULT WINAPI QarTestWrapEnumerator(IUnknown* raw, const qar::ConfigV1* config,
                                                 IMMDeviceEnumerator** wrapped) {
    return qar::WrapEnumerator(raw, config, wrapped);
}
#endif
