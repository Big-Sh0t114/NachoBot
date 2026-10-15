"use strict";

// Offline SDK enum/caller audit: only private scene 1 is in scope; commands
// 8/9/10 are the pinned QRTC Quit/Reject/Close operations.
const PRIVATE_CALL_SCENE = 1;
const QRTC_COMMAND_QUIT = 8;
const QRTC_COMMAND_REJECT = 9;
const QRTC_COMMAND_CLOSE = 10;
const QRTC_QUIT_REASON_NONE = 0;
// The pinned CallStore.onC2CReject caller uses QRTCRejectReasonDefault.
const QRTC_REJECT_REASON_DEFAULT = 1;
const QRTC_REJECT_REASON_SELF_REJECT = 2;
const QRTC_SELF_CLOSE_REASON_DEFAULT = 0;
const QQ_UID = /^u_[A-Za-z0-9_-]{22}$/;

function buildPrivateHangupParams() {
  return [PRIVATE_CALL_SCENE, QRTC_QUIT_REASON_NONE];
}

function buildPrivateRejectParams(peerUid) {
  if (typeof peerUid !== "string" || !QQ_UID.test(peerUid) || /[\u0000-\u001f\u007f]/.test(peerUid)) {
    const error = new Error("invalid_private_peer_uid");
    error.code = "invalid_private_peer_uid";
    throw error;
  }
  return [PRIVATE_CALL_SCENE, peerUid, QRTC_REJECT_REASON_DEFAULT];
}

function buildPrivateCloseParams(peerUid) {
  if (typeof peerUid !== "string" || !QQ_UID.test(peerUid) || /[\u0000-\u001f\u007f]/.test(peerUid)) {
    const error = new Error("invalid_private_peer_uid");
    error.code = "invalid_private_peer_uid";
    throw error;
  }
  return [PRIVATE_CALL_SCENE, peerUid, QRTC_SELF_CLOSE_REASON_DEFAULT];
}

function validatePrivateEndParams(command, params) {
  if (!Array.isArray(params)) return false;
  if (command === QRTC_COMMAND_QUIT) {
    return params.length === 2 && params[0] === PRIVATE_CALL_SCENE &&
      params[1] === QRTC_QUIT_REASON_NONE;
  }
  if (command === QRTC_COMMAND_REJECT) {
    return params.length === 3 && params[0] === PRIVATE_CALL_SCENE &&
      typeof params[1] === "string" && QQ_UID.test(params[1]) &&
      !/[\u0000-\u001f\u007f]/.test(params[1]) &&
      params[2] === QRTC_REJECT_REASON_DEFAULT;
  }
  if (command === QRTC_COMMAND_CLOSE) {
    return params.length === 3 && params[0] === PRIVATE_CALL_SCENE &&
      typeof params[1] === "string" && QQ_UID.test(params[1]) &&
      !/[\u0000-\u001f\u007f]/.test(params[1]) &&
      params[2] === QRTC_SELF_CLOSE_REASON_DEFAULT;
  }
  return false;
}

module.exports = {
  PRIVATE_CALL_SCENE,
  QRTC_COMMAND_QUIT,
  QRTC_COMMAND_REJECT,
  QRTC_COMMAND_CLOSE,
  QRTC_QUIT_REASON_NONE,
  QRTC_REJECT_REASON_SELF_REJECT,
  QRTC_REJECT_REASON_DEFAULT,
  QRTC_SELF_CLOSE_REASON_DEFAULT,
  buildPrivateHangupParams,
  buildPrivateRejectParams,
  buildPrivateCloseParams,
  validatePrivateEndParams,
};
