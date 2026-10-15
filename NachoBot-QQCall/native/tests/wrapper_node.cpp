typedef unsigned char BYTE;
typedef unsigned short WORD;
typedef unsigned long DWORD;
typedef unsigned long long SIZE_T;
typedef int BOOL;
typedef void* napi_env;
typedef void* napi_value;
typedef void* napi_callback_info;
typedef void* HINSTANCE;
typedef void* LPVOID;
#define WINAPI __stdcall
#define TRUE 1
#define FALSE 0
#define DLL_PROCESS_ATTACH 1
#define PAGE_READWRITE 0x04

extern "C" __declspec(dllimport) int WINAPI napi_get_cb_info(napi_env, napi_callback_info, SIZE_T*, napi_value*, napi_value*, void**);
extern "C" __declspec(dllimport) BOOL WINAPI VirtualProtect(LPVOID, SIZE_T, DWORD, DWORD*);
extern "C" void* __cdecl _InterlockedCompareExchangePointer(void* volatile*, void*, void*);
#pragma intrinsic(_InterlockedCompareExchangePointer)
#define InterlockedCompareExchangePointer _InterlockedCompareExchangePointer
extern "C" BYTE __ImageBase;

static void* g_foreignOriginal = 0;

static int WINAPI ForeignGetCbInfo(napi_env, napi_callback_info, SIZE_T*, napi_value*, napi_value*, void**) {
    return 77;
}

static WORD Read16(const BYTE* p) { return (WORD)(p[0] | ((WORD)p[1] << 8)); }
static DWORD Read32(const BYTE* p) { return (DWORD)p[0] | ((DWORD)p[1] << 8) | ((DWORD)p[2] << 16) | ((DWORD)p[3] << 24); }
static unsigned long long Read64(const BYTE* p) { return (unsigned long long)Read32(p) | ((unsigned long long)Read32(p + 4) << 32); }

static BOOL EqualsQqntName(const char* name) {
    const char wanted[] = "QQNT.dll";
    DWORD i = 0;
    for (; wanted[i]; ++i) {
        char c = name[i];
        if (c >= 'a' && c <= 'z') c = (char)(c - ('a' - 'A'));
        char w = wanted[i];
        if (w >= 'a' && w <= 'z') w = (char)(w - ('a' - 'A'));
        if (c != w) return FALSE;
    }
    return name[i] == 0;
}

static void** FindCbInfoImport(void) {
    BYTE* base = &__ImageBase;
    if (Read16(base) != 0x5A4D) return 0;
    DWORD ntRva = Read32(base + 0x3c);
    if (ntRva > 0x100000) return 0;
    BYTE* nt = base + ntRva;
    if (Read32(nt) != 0x00004550 || Read16(nt + 4) != 0x8664) return 0;
    WORD optionalSize = Read16(nt + 20);
    BYTE* optional = nt + 24;
    if (optionalSize < 136 || Read16(optional) != 0x20b) return 0;
    DWORD imageSize = Read32(optional + 56);
    DWORD importRva = Read32(optional + 120);
    DWORD importSize = Read32(optional + 124);
    if (imageSize < 0x1000 || !importRva || importRva >= imageSize || importSize < 20 || importSize > imageSize - importRva) return 0;
    DWORD importEnd = importRva + importSize;
    for (DWORD rva = importRva; rva <= importEnd - 20; rva += 20) {
        BYTE* descriptor = base + rva;
        DWORD oft = Read32(descriptor);
        DWORD nameRva = Read32(descriptor + 12);
        DWORD first = Read32(descriptor + 16);
        if (!oft && !nameRva && !first) break;
        if (!oft || !nameRva || !first || nameRva >= imageSize || first >= imageSize) continue;
        const char* dllName = (const char*)(base + nameRva);
        DWORD nameLength = 0;
        for (; nameLength < imageSize - nameRva && dllName[nameLength]; ++nameLength) {}
        if (nameLength == imageSize - nameRva || !EqualsQqntName(dllName)) continue;
        for (DWORD index = 0; index < imageSize / 8; ++index) {
            DWORD tableOffset = index * 8;
            if (oft > imageSize - 8 || tableOffset > imageSize - oft - 8 ||
                first > imageSize - 8 || tableOffset > imageSize - first - 8) return 0;
            unsigned long long thunk = Read64(base + oft + tableOffset);
            if (!thunk) break;
            if (thunk & 0x8000000000000000ULL) continue;
            if (thunk > imageSize - 3) return 0;
            DWORD hintNameRva = (DWORD)thunk;
            const char* functionName = (const char*)(base + hintNameRva + 2);
            const char wanted[] = "napi_get_cb_info";
            DWORD i = 0;
            DWORD maxNameLength = imageSize - hintNameRva - 2;
            for (; wanted[i] && i < maxNameLength && functionName[i] == wanted[i]; ++i) {}
            if (i == maxNameLength || wanted[i] || functionName[i] != 0) continue;
            void** slot = (void**)(base + first + tableOffset);
            if (((unsigned long long)slot & 7ULL) != 0) return 0;
            return slot;
        }
    }
    return 0;
}

extern "C" void* WINAPI WrapperGetCbInfoImport(void) {
    void** slot = FindCbInfoImport();
    return slot ? *slot : 0;
}

extern "C" int WINAPI WrapperInvoke(napi_env env) {
    return napi_get_cb_info(env, 0, 0, 0, 0, 0);
}

extern "C" int WINAPI WrapperInstallForeign(void) {
    BYTE* base = &__ImageBase;
    if (Read16(base) != 0x5A4D) return 1;
    DWORD ntRva = Read32(base + 0x3c);
    BYTE* file = base + ntRva + 4;
    BYTE* optional = file + 20;
    DWORD importRva = Read32(optional + 120);
    DWORD importSize = Read32(optional + 124);
    DWORD imageSize = Read32(optional + 56);
    if (!importRva || !importSize || importRva >= imageSize || importSize > imageSize - importRva) return 2;
    for (DWORD rva = importRva; rva + 20 <= importRva + importSize; rva += 20) {
        BYTE* desc = base + rva;
        DWORD oft = Read32(desc);
        DWORD nameRva = Read32(desc + 12);
        DWORD first = Read32(desc + 16);
        if (!oft && !nameRva && !first) break;
        const char* dll = (const char*)(base + nameRva);
        if (!EqualsQqntName(dll)) continue;
        DWORD nameTable = oft ? oft : first;
        if (!oft) return 3;
        for (DWORD index = 0; index < imageSize / 8; ++index) {
            unsigned long long thunk = Read64(base + nameTable + index * 8);
            if (!thunk) break;
            if (thunk & 0x8000000000000000ULL) continue;
            const char* name = (const char*)(base + (DWORD)thunk + 2);
            const char wanted[] = "napi_get_cb_info";
            BOOL match = TRUE;
            DWORD n = 0;
            for (; wanted[n]; ++n) if (name[n] != wanted[n]) { match = FALSE; break; }
            if (!match || name[n] != 0) continue;
            void** slot = (void**)(base + first + index * 8);
            DWORD oldProtect = 0;
            if (!VirtualProtect(slot, 8, PAGE_READWRITE, &oldProtect)) return 4;
            g_foreignOriginal = *slot;
            void* replaced = InterlockedCompareExchangePointer((void* volatile*)slot, (void*)&ForeignGetCbInfo, g_foreignOriginal);
            DWORD ignored = 0;
            VirtualProtect(slot, 8, oldProtect, &ignored);
            return replaced == g_foreignOriginal ? 0 : 5;
        }
        return 6;
    }
    return 7;
}

extern "C" int WINAPI DllMain(HINSTANCE, DWORD, LPVOID) { return TRUE; }
