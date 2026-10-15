#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace qar {

enum class CfgEntryResult {
    valid,
    missing,
    suppressed,
    unsupported_metadata,
    malformed,
};

constexpr uint8_t kCfgFidSuppressed = 0x01;
constexpr uint8_t kCfgExportSuppressed = 0x02;
constexpr uint8_t kCfgKnownFlagMask = kCfgFidSuppressed | kCfgExportSuppressed;

inline CfgEntryResult FindCfgEntry(const uint8_t* table, size_t count, size_t stride,
                                   uint32_t targetRva, bool resolvedByGetProcAddress) {
    if (!table || !count || count > 1000000 || (stride != 4 && stride != 5))
        return CfgEntryResult::malformed;
    uint32_t low = 0;
    uint32_t high = static_cast<uint32_t>(count);
    while (low < high) {
        const uint32_t mid = low + (high - low) / 2;
        uint32_t entryRva = 0;
        std::memcpy(&entryRva, table + static_cast<size_t>(mid) * stride, sizeof(entryRva));
        if (entryRva < targetRva) low = mid + 1;
        else high = mid;
    }
    if (low >= count) return CfgEntryResult::missing;
    uint32_t entryRva = 0;
    const uint8_t* entry = table + static_cast<size_t>(low) * stride;
    std::memcpy(&entryRva, entry, sizeof(entryRva));
    if (entryRva != targetRva) return CfgEntryResult::missing;
    if (stride == 4) return CfgEntryResult::valid;

    const uint8_t flags = entry[4];
    if (flags & ~kCfgKnownFlagMask) return CfgEntryResult::unsupported_metadata;
    if (flags & kCfgFidSuppressed) return CfgEntryResult::suppressed;
    if ((flags & kCfgExportSuppressed) && !resolvedByGetProcAddress)
        return CfgEntryResult::suppressed;
    return CfgEntryResult::valid;
}

} // namespace qar
