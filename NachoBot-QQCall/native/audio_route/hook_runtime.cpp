#include "hook_runtime.h"
#include "cfg_metadata.h"

#include "vendor/minhook/include/MinHook.h"

#include <processthreadsapi.h>

#include <algorithm>
#include <new>
#include <vector>

namespace qar {
namespace {

bool ReadImageCfgTable(HMODULE image, const DWORD** table, DWORD* count, size_t* stride) {
    if (table) *table = nullptr;
    if (count) *count = 0;
    if (stride) *stride = 0;
    if (!image || !table || !count || !stride) return false;

    const auto* base = reinterpret_cast<const BYTE*>(image);
    const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 || dos->e_lfanew > 0x100000) return false;
    const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC ||
        nt->OptionalHeader.SizeOfImage < sizeof(IMAGE_DOS_HEADER)) return false;
    const IMAGE_DATA_DIRECTORY& directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG];
    if (!directory.VirtualAddress || directory.Size < sizeof(DWORD)) return false;
    if (directory.VirtualAddress > nt->OptionalHeader.SizeOfImage ||
        directory.Size > nt->OptionalHeader.SizeOfImage - directory.VirtualAddress) return false;
    const auto* load = reinterpret_cast<const IMAGE_LOAD_CONFIG_DIRECTORY64*>(base + directory.VirtualAddress);
    if (load->Size < offsetof(IMAGE_LOAD_CONFIG_DIRECTORY64, GuardFlags) + sizeof(DWORD)) return false;
    if (!(load->GuardFlags & IMAGE_GUARD_CF_INSTRUMENTED) ||
        !(load->GuardFlags & IMAGE_GUARD_CF_FUNCTION_TABLE_PRESENT) ||
        !load->GuardCFFunctionTable || !load->GuardCFFunctionCount || load->GuardCFFunctionCount > 1000000) return false;

    const DWORD entryExtra = (load->GuardFlags & IMAGE_GUARD_CF_FUNCTION_TABLE_SIZE_MASK) >>
                             IMAGE_GUARD_CF_FUNCTION_TABLE_SIZE_SHIFT;
    const size_t entryStride = sizeof(DWORD) + entryExtra;
    if (entryStride != 4 && entryStride != 5) return false;
    if (load->GuardCFFunctionCount > SIZE_MAX / entryStride) return false;
    const DWORD* entries = reinterpret_cast<const DWORD*>(load->GuardCFFunctionTable);
    const uintptr_t first = reinterpret_cast<uintptr_t>(base);
    const uintptr_t ptr = reinterpret_cast<uintptr_t>(entries);
    const size_t bytes = static_cast<size_t>(load->GuardCFFunctionCount) * entryStride;
    if (ptr < first || ptr - first > nt->OptionalHeader.SizeOfImage ||
        bytes > nt->OptionalHeader.SizeOfImage - (ptr - first)) return false;
    *table = entries;
    *count = static_cast<DWORD>(load->GuardCFFunctionCount);
    *stride = entryStride;
    return true;
}

class MinHookBackend final : public HookBackend {
public:
    bool initialize() override {
        const MH_STATUS status = MH_Initialize();
        return status == MH_OK || status == MH_ERROR_ALREADY_INITIALIZED;
    }

    bool create(HookRequest* request) override {
        if (!request || !request->module || !request->exportName || !request->detour) return false;
        request->target = ResolveSystemExport(request->module, request->exportName);
        if (!request->target) return false;
        const MH_STATUS status = MH_CreateHook(request->target, request->detour, &request->original);
        if (status != MH_OK) return false;
        request->created = true;
        return true;
    }

    bool markControlFlowTargets(HookRequest* request) override {
        if (!request || !request->target || !request->detour) return false;
        PROCESS_MITIGATION_CONTROL_FLOW_GUARD_POLICY policy = {};
        if (!GetProcessMitigationPolicy(GetCurrentProcess(), ProcessControlFlowGuardPolicy,
                                        &policy, sizeof(policy)) || !policy.EnableControlFlowGuard) return false;
        return IsCfgImageTarget(request->target, true) && IsCfgImageTarget(request->detour, false);
    }

    bool queueEnable(HookRequest* request) override {
        return request && request->created && MH_QueueEnableHook(request->target) == MH_OK;
    }

    bool applyQueued() override { return MH_ApplyQueued() == MH_OK; }

    void rollback(HookRequest* requests, size_t count) override {
        if (!requests) return;
        for (size_t i = 0; i < count; ++i) {
            if (requests[i].created) {
                MH_DisableHook(requests[i].target);
                MH_RemoveHook(requests[i].target);
                requests[i].created = false;
                requests[i].original = nullptr;
                requests[i].target = nullptr;
            }
        }
        MH_ApplyQueued();
    }
};

} // namespace

bool VerifyCurrentProcessCfg() {
    PROCESS_MITIGATION_CONTROL_FLOW_GUARD_POLICY policy = {};
    if (!GetProcessMitigationPolicy(GetCurrentProcess(), ProcessControlFlowGuardPolicy,
                                    &policy, sizeof(policy)) || !policy.EnableControlFlowGuard) return false;
    HMODULE self = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(&VerifyCurrentProcessCfg), &self)) return false;
    const DWORD* table = nullptr;
    DWORD count = 0;
    size_t stride = 0;
    return ReadImageCfgTable(self, &table, &count, &stride);
}

bool IsCfgImageTarget(const void* address, bool resolvedByGetProcAddress) {
    if (!address) return false;
    HMODULE image = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(address), &image)) return false;
    const DWORD* table = nullptr;
    DWORD count = 0;
    size_t stride = 0;
    if (!ReadImageCfgTable(image, &table, &count, &stride)) return false;
    const uintptr_t base = reinterpret_cast<uintptr_t>(image);
    const uintptr_t target = reinterpret_cast<uintptr_t>(address);
    if (target < base || target - base > MAXDWORD) return false;
    const DWORD wanted = static_cast<DWORD>(target - base);
    const CfgEntryResult entry = FindCfgEntry(reinterpret_cast<const uint8_t*>(table), count,
                                               stride, wanted, resolvedByGetProcAddress);
    return entry == CfgEntryResult::valid;
}

void* ResolveSystemExport(const wchar_t* moduleName, const char* exportName) {
    if (!moduleName || !exportName) return nullptr;
    HMODULE module = GetModuleHandleW(moduleName);
    if (!module) module = LoadLibraryW(moduleName);
    return module ? reinterpret_cast<void*>(GetProcAddress(module, exportName)) : nullptr;
}

HookBackend* CreateMinHookBackend() { return new (std::nothrow) MinHookBackend(); }
void DestroyMinHookBackend(HookBackend* backend) { delete backend; }

} // namespace qar
