#pragma once

#include "route_abi.h"

namespace qar {

const ConfigV1* ActiveChildConfig();
StatusV1* ActiveChildStatus();
void CountInstalledHooks(uint64_t value);
void CountReentry();
void CountRejectedActivation();
inline void RecordDiagnostic(DiagnosticStage stage, HRESULT result) {
    StatusV1* status = ActiveChildStatus();
    const uint32_t index = static_cast<uint32_t>(stage);
    if (!status || index >= static_cast<uint32_t>(DiagnosticStage::count)) return;
    InterlockedIncrement64(reinterpret_cast<volatile LONG64*>(&status->diagnosticCounts[index]));
    InterlockedExchange(reinterpret_cast<volatile LONG*>(&status->diagnosticLastHresults[index]), result);
}
inline void RecordInterfaceDiagnostic(REFIID iid, bool activation, uint32_t flow = 2) {
    StatusV1* status = ActiveChildStatus();
    if (!status) return;
    // Public COM IIDs only. These best-effort hints are never readiness or
    // attribution evidence and do not contain instance pointers or device IDs.
    uint64_t words[2] = {};
    CopyMemory(words, &iid, sizeof(iid));
    GUID* target = activation ? &status->lastActivationIid : &status->lastUnsupportedIid;
    auto slots = reinterpret_cast<volatile LONG64*>(target);
    InterlockedExchange64(&slots[0], static_cast<LONG64>(words[0]));
    InterlockedExchange64(&slots[1], static_cast<LONG64>(words[1]));
    if (activation) InterlockedExchange(reinterpret_cast<volatile LONG*>(&status->lastActivationFlow), flow);
}

} // namespace qar
