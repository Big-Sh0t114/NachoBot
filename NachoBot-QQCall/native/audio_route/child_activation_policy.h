#pragma once

namespace qar {

// Reentrancy is permitted for unrelated COM work. A nested attempt to acquire
// the guarded enumerator must fail closed instead of reaching the raw factory.
inline bool DenyReentrantEnumeratorActivation(bool reentrant, bool targetEnumerator) {
    return reentrant && targetEnumerator;
}

} // namespace qar
