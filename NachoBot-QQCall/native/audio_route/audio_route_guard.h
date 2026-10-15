#pragma once

#include "route_abi.h"

#include <mmdeviceapi.h>

namespace qar {

HRESULT InitializeGlobalInterfaceTable();
HRESULT WrapEnumerator(IUnknown* raw, const ConfigV1* config, IMMDeviceEnumerator** wrapped);
HRESULT ValidateConfiguredEndpoints(IMMDeviceEnumerator* raw, const ConfigV1* config);
HRESULT WrapRequestedEnumerator(REFIID iid, IUnknown* raw, const ConfigV1* config, void** result);
bool InstallChildApiHooks();
void SetChildStatus(StatusV1* status);

} // namespace qar

#if defined(QAR_TESTING)
extern "C" __declspec(dllexport) HRESULT WINAPI QarTestWrapEnumerator(
    IUnknown* raw, const qar::ConfigV1* config, IMMDeviceEnumerator** wrapped);
#endif
