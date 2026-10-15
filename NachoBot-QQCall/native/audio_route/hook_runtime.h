#pragma once

#include "hook_transaction.h"

#include <windows.h>

namespace qar {

bool VerifyCurrentProcessCfg();
bool IsCfgImageTarget(const void* address, bool resolvedByGetProcAddress = false);
void* ResolveSystemExport(const wchar_t* moduleName, const char* exportName);
HookBackend* CreateMinHookBackend();
void DestroyMinHookBackend(HookBackend* backend);

} // namespace qar
