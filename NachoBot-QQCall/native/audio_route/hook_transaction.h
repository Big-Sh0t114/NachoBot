#pragma once

#include <cstddef>

namespace qar {

struct HookRequest {
    const wchar_t* module;
    const char* exportName;
    void* detour;
    void* original;
    void* target;
    bool created;
};

class HookBackend {
public:
    virtual ~HookBackend() = default;
    virtual bool initialize() = 0;
    virtual bool create(HookRequest* request) = 0;
    virtual bool markControlFlowTargets(HookRequest* request) = 0;
    virtual bool queueEnable(HookRequest* request) = 0;
    virtual bool applyQueued() = 0;
    virtual void rollback(HookRequest* requests, size_t count) = 0;
};

inline bool InstallHookSet(HookBackend* backend, HookRequest* requests, size_t count) {
    if (!backend || !requests || count == 0 || !backend->initialize()) return false;
    size_t created = 0;
    for (; created < count; ++created) {
        HookRequest* request = &requests[created];
        if (!backend->create(request) || !request->original || !request->target ||
            !backend->markControlFlowTargets(request)) {
            backend->rollback(requests, created + (request->created ? 1 : 0));
            return false;
        }
    }
    for (size_t i = 0; i < count; ++i) {
        if (!backend->queueEnable(&requests[i])) {
            backend->rollback(requests, count);
            return false;
        }
    }
    if (!backend->applyQueued()) {
        backend->rollback(requests, count);
        return false;
    }
    return true;
}

} // namespace qar
