"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("qqCallHostBridge", Object.freeze({
  reportReady(value) {
    ipcRenderer.send("nachobot-qqcall-host:ready", value);
  },
  publishOutput(value) {
    ipcRenderer.send("nachobot-qqcall-host:output", value);
  },
  reportFailure(code) {
    ipcRenderer.send("nachobot-qqcall-host:failure", code);
  },
}));
