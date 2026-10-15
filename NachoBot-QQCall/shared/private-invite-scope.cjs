"use strict";

const PRIVATE_INVITE_PROFILE = "qq-9.9.26-44343-c2c-invite-type-1";
const PINNED_WRAPPER_SHA256 = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";
const FILETIME_MAX = 0xffffffffffffffffn;
const NATIVE_EVIDENCE_KEYS = Object.freeze([
  "profile", "wrapperSha256", "callbackSequence", "listenerRegistrationSequence", "inviteType", "relationId", "fromUid",
]);
const NATIVE_PROVENANCE_KEYS = Object.freeze([
  "targetPid", "kernelCreationTime100ns", "connectionGeneration", "listenerMutationEpoch", "ipcSequence",
]);
const PROJECTED_PROOF_KEYS = Object.freeze([
  "version", "profile", "wrapperSha256", "callbackSequence", "inviteType", "relationId", "fromUid",
  "targetPid", "kernelCreationTime100ns", "connectionGeneration", "listenerMutationEpoch", "ipcSequence",
  "accountId", "serviceInstance", "hostGeneration", "scopeKind", "callId", "callGeneration", "inviteEventId",
]);

function ownData(object, key) {
  if (!object || typeof object !== "object") return undefined;
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && "value" in descriptor ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function hasExactKeys(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  let actual;
  try { actual = Object.keys(value).sort(); }
  catch { return false; }
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
}

function positiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function canonicalFiletime(value) {
  if (typeof value !== "string" || !/^[1-9][0-9]{0,19}$/.test(value)) return false;
  try { return BigInt(value) <= FILETIME_MAX; }
  catch { return false; }
}

function safeNativeIdentifier(value, maximum = 128) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum &&
    /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value);
}

function safeNativeUid(value) {
  // The pinned legacy C2C producer supplies an actual empty from_uid string.
  // Missing, null, accessor-backed, and malformed values remain unprovable.
  return value === "" || safeNativeIdentifier(value);
}

function nativeInviteEvidenceFromCallback(profile, method, args, callbackSequence, listenerRegistrationSequence) {
  if (profile !== PRIVATE_INVITE_PROFILE || typeof method !== "string" ||
      method.toLowerCase() !== "oninviteactiontoavsdk" || !Array.isArray(args) ||
      !positiveSafeInteger(callbackSequence) || !positiveSafeInteger(listenerRegistrationSequence)) return null;
  const value = ownData(args, "0");
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const inviteType = ownData(value, "invite_type");
  const relationId = ownData(value, "relation_id");
  const fromUid = ownData(value, "from_uid");
  if (inviteType !== 1 || !safeNativeIdentifier(relationId, 256) || !safeNativeUid(fromUid)) return null;
  return Object.freeze({
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    callbackSequence,
    listenerRegistrationSequence,
    inviteType: 1,
    relationId,
    fromUid,
  });
}

function validateNativeInviteEvidence(evidence, event, currentListenerBinding = null) {
  if (!hasExactKeys(evidence, NATIVE_EVIDENCE_KEYS) || !event || typeof event !== "object" ||
      typeof event.method !== "string" || event.method.toLowerCase() !== "oninviteactiontoavsdk" ||
      !Array.isArray(event.args)) return null;
  const callbackSequence = ownData(evidence, "callbackSequence");
  const listenerRegistrationSequence = ownData(evidence, "listenerRegistrationSequence");
  const relationId = ownData(evidence, "relationId");
  const fromUid = ownData(evidence, "fromUid");
  const args = ownData(event, "args");
  const value = ownData(args, "0");
  if (ownData(evidence, "profile") !== PRIVATE_INVITE_PROFILE ||
      ownData(evidence, "wrapperSha256") !== PINNED_WRAPPER_SHA256 ||
      ownData(evidence, "inviteType") !== 1 || !positiveSafeInteger(callbackSequence) ||
      !positiveSafeInteger(listenerRegistrationSequence) ||
      (currentListenerBinding && listenerRegistrationSequence !== ownData(currentListenerBinding, "listenerRegistrationSequence")) ||
      !safeNativeIdentifier(relationId, 256) || !safeNativeUid(fromUid) ||
      ownData(value, "invite_type") !== 1 || ownData(value, "relation_id") !== relationId ||
      ownData(value, "from_uid") !== fromUid) return null;
  return Object.freeze({
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    callbackSequence,
    listenerRegistrationSequence,
    inviteType: 1,
    relationId,
    fromUid,
  });
}

function validateNativeInviteProvenance(provenance, binding, ipcSequence) {
  if (!hasExactKeys(provenance, NATIVE_PROVENANCE_KEYS) || !binding || typeof binding !== "object") return null;
  const normalized = {
    targetPid: ownData(provenance, "targetPid"),
    kernelCreationTime100ns: ownData(provenance, "kernelCreationTime100ns"),
    connectionGeneration: ownData(provenance, "connectionGeneration"),
    listenerMutationEpoch: ownData(provenance, "listenerMutationEpoch"),
    ipcSequence: ownData(provenance, "ipcSequence"),
  };
  if (!Number.isSafeInteger(normalized.targetPid) || normalized.targetPid <= 0 ||
      !canonicalFiletime(normalized.kernelCreationTime100ns) ||
      !positiveSafeInteger(normalized.connectionGeneration) ||
      !positiveSafeInteger(normalized.listenerMutationEpoch) ||
      !positiveSafeInteger(normalized.ipcSequence) || normalized.ipcSequence !== ipcSequence ||
      normalized.targetPid !== ownData(binding, "targetPid") ||
      normalized.kernelCreationTime100ns !== ownData(binding, "kernelCreationTime100ns") ||
      normalized.connectionGeneration !== ownData(binding, "connectionGeneration") ||
      normalized.listenerMutationEpoch !== ownData(binding, "listenerMutationEpoch") ||
      ownData(binding, "profile") !== PRIVATE_INVITE_PROFILE ||
      ownData(binding, "wrapperSha256") !== PINNED_WRAPPER_SHA256) return null;
  return Object.freeze(normalized);
}

function projectPrivateScopeProof(evidence, provenance, context) {
  if (!evidence || !provenance || !context || typeof context !== "object") return null;
  const scopeKind = context.scopeKind;
  const identityCorrelation = context.identityCorrelation;
  const resolvedPeerUid = context.resolvedPeerUid;
  const callId = context.callId ?? null;
  const callGeneration = context.callGeneration ?? null;
  if (!safeNativeIdentifier(context.accountId, 20) || !safeNativeIdentifier(context.serviceInstance, 64) ||
      !positiveSafeInteger(context.hostGeneration) || !positiveSafeInteger(context.connectionGeneration) ||
      (scopeKind === "call" && (!safeNativeIdentifier(callId, 128) || !positiveSafeInteger(callGeneration))) ||
      (scopeKind === "busy" && (callId !== null || callGeneration !== null)) ||
      !["call", "busy"].includes(scopeKind) || !safeNativeIdentifier(context.inviteEventId, 64)) return null;
  if (evidence.fromUid === "") {
    if (identityCorrelation !== "kernel_relation_uid_matched" ||
        !/^[1-9][0-9]{0,19}$/.test(evidence.relationId) ||
        !safeNativeIdentifier(resolvedPeerUid)) return null;
  } else if (identityCorrelation !== "kernel_from_uid_matched" || evidence.fromUid !== resolvedPeerUid) {
    return null;
  }
  const proof = {
    version: 1,
    profile: evidence.profile,
    wrapperSha256: evidence.wrapperSha256,
    callbackSequence: evidence.callbackSequence,
    inviteType: evidence.inviteType,
    relationId: evidence.relationId,
    fromUid: evidence.fromUid,
    targetPid: provenance.targetPid,
    kernelCreationTime100ns: provenance.kernelCreationTime100ns,
    connectionGeneration: provenance.connectionGeneration,
    listenerMutationEpoch: provenance.listenerMutationEpoch,
    ipcSequence: provenance.ipcSequence,
    accountId: context.accountId,
    serviceInstance: context.serviceInstance,
    hostGeneration: context.hostGeneration,
    scopeKind,
    callId,
    callGeneration,
    inviteEventId: context.inviteEventId,
  };
  if (!hasExactKeys(proof, PROJECTED_PROOF_KEYS) ||
      proof.profile !== PRIVATE_INVITE_PROFILE || proof.wrapperSha256 !== PINNED_WRAPPER_SHA256 ||
      proof.inviteType !== 1 || !safeNativeIdentifier(proof.relationId, 256) ||
      !safeNativeUid(proof.fromUid) || !Number.isSafeInteger(proof.targetPid) || proof.targetPid <= 0 ||
      !canonicalFiletime(proof.kernelCreationTime100ns) ||
      proof.connectionGeneration !== context.connectionGeneration ||
      !positiveSafeInteger(proof.listenerMutationEpoch) || !positiveSafeInteger(proof.ipcSequence) ||
      !positiveSafeInteger(proof.callbackSequence)) return null;
  return Object.freeze(proof);
}

module.exports = {
  PRIVATE_INVITE_PROFILE,
  PINNED_WRAPPER_SHA256,
  canonicalFiletime,
  hasExactKeys,
  nativeInviteEvidenceFromCallback,
  positiveSafeInteger,
  projectPrivateScopeProof,
  safeNativeIdentifier,
  validateNativeInviteEvidence,
  validateNativeInviteProvenance,
};
