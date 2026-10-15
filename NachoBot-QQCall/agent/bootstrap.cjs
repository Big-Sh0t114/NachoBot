"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const { createKernelAgent } = require("./kernel-agent.cjs");
const { probeRendererCapabilities } = require("./renderer-capability-probe.cjs");
const { probeAsarSources } = require("./asar-source-probe.cjs");
const { probeAsarCandidates } = require("./asar-candidate-probe.cjs");
const { probeOfficialWindows } = require("./official-window-probe.cjs");
const { probeRendererScriptMetadata } = require("./renderer-script-metadata-probe.cjs");
const { createOfficialAVObserver } = require("./official-av-observer.cjs");
const { createOfficialPrivateInviteController } = require("./official-private-invite.cjs");
const { codedError, createPipeClient, equalToken, isGeneration, validToken, validatePipeName } = require("../probe/ipc.cjs");
const { PRIVATE_INVITE_PROFILE, PINNED_WRAPPER_SHA256 } = require("../shared/private-invite-scope.cjs");

const STATE_KEY = Symbol.for("nachobot.qqcall.bootstrap.v1");
const PINNED_LISTENER_OBJECT_WRAPPER_SHA256 = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";

function wrapperAccessor(exports) {
  return exports?.NodeIQQNTWrapperSession && typeof exports.NodeIQQNTWrapperSession.getNTWrapperSession === "function"
    ? exports
    : null;
}

function insidePath(candidate, root) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

function samePath(left, right) {
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function wrapperFromCache(expectedModulePath) {
  const caches = [require.cache, process.mainModule?.require?.cache].filter(Boolean);
  const seen = new Set();
  for (const cache of caches) {
    for (const entry of Object.values(cache)) {
      if (!entry || seen.has(entry)) continue;
      seen.add(entry);
      const found = wrapperAccessor(entry.exports);
      if (!found) continue;
      const modulePath = typeof entry.filename === "string" ? entry.filename : typeof entry.id === "string" && path.isAbsolute(entry.id) ? entry.id : null;
      if (expectedModulePath && (!modulePath || !samePath(modulePath, expectedModulePath))) continue;
      return { wrapper: found, modulePath };
    }
  }
  return null;
}

function loadWrapper(config) {
  const cached = wrapperFromCache(config.wrapperModulePath);
  if (cached) return { ...cached, source: "cache" };
  if (wrapperFromCache()) throw codedError("wrapper_cache_path_mismatch");
  const modulePath = config.wrapperModulePath;
  if (!modulePath) throw codedError("wrapper_not_cached");
  if (typeof modulePath !== "string" || !path.isAbsolute(modulePath) || !insidePath(modulePath, config.appPath)) {
    throw codedError("wrapper_module_outside_app_path");
  }
  if (path.extname(modulePath).toLowerCase() !== ".node") throw codedError("wrapper_module_not_native");
  let stats;
  try { stats = fs.statSync(modulePath); }
  catch { throw codedError("wrapper_module_missing"); }
  if (!stats.isFile()) throw codedError("wrapper_module_missing");
  if (typeof process.dlopen !== "function") throw codedError("process_dlopen_unavailable");

  const nativeModule = new Module(modulePath, module);
  nativeModule.filename = modulePath;
  nativeModule.paths = Module._nodeModulePaths(path.dirname(modulePath));
  try { process.dlopen(nativeModule, modulePath); }
  catch { throw codedError("wrapper_dlopen_failed"); }
  nativeModule.loaded = true;
  require.cache[modulePath] = nativeModule;
  const wrapper = wrapperAccessor(nativeModule.exports);
  if (!wrapper) throw codedError("wrapper_export_unavailable");
  return { wrapper, modulePath, source: "dlopen" };
}

function removalModeForDigest(digest) {
  return typeof digest === "string" && digest.toUpperCase() === PINNED_LISTENER_OBJECT_WRAPPER_SHA256
    ? "listener-object"
    : "unverified";
}

function selectRemovalContract(config, loadedModulePath = config.wrapperModulePath) {
  const modulePath = loadedModulePath;
  if (config.wrapperModulePath && (!loadedModulePath || !samePath(config.wrapperModulePath, loadedModulePath))) {
    return { mode: "unverified", verified: false };
  }
  if (typeof modulePath !== "string" || !path.isAbsolute(modulePath) || !insidePath(modulePath, config.appPath) || path.extname(modulePath).toLowerCase() !== ".node") {
    return { mode: "unverified", verified: false };
  }
  let digest;
  try { digest = crypto.createHash("sha256").update(fs.readFileSync(modulePath)).digest("hex").toUpperCase(); }
  catch { return { mode: "unverified", verified: false }; }
  const mode = removalModeForDigest(digest);
  const verified = mode === "listener-object" && digest === PINNED_WRAPPER_SHA256;
  return Object.freeze({
    mode,
    verified,
    nativeInviteScopeProfile: verified ? PRIVATE_INVITE_PROFILE : null,
  });
}

function validateConfig(input) {
  const config = input || globalThis.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__;
  if (!config || typeof config !== "object") throw codedError("bootstrap_config_missing");
  if (typeof config.modulePath !== "string" || !path.isAbsolute(config.modulePath)) throw codedError("bootstrap_module_path_invalid");
  if (typeof config.appPath !== "string" || !path.isAbsolute(config.appPath)) throw codedError("app_path_invalid");
  validatePipeName(config.pipeName);
  if (!isGeneration(config.connectionGeneration)) throw codedError("generation_invalid");
  if (typeof config.sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(config.sessionName)) throw codedError("session_name_invalid");
  if (!validToken(config.ipcToken)) throw codedError("auth_token_invalid");
  if (config.wrapperModulePath !== undefined && (typeof config.wrapperModulePath !== "string" || !path.isAbsolute(config.wrapperModulePath))) {
    throw codedError("wrapper_module_path_invalid");
  }
  if (config.reportPath !== undefined && (typeof config.reportPath !== "string" || !path.isAbsolute(config.reportPath))) {
    throw codedError("report_path_invalid");
  }
  if (config.attach === true) throw codedError("explicit_attach_required");
  return config;
}

function sameRun(left, right) {
  return left.connectionGeneration === right.connectionGeneration &&
    left.pipeName === right.pipeName &&
    left.modulePath === right.modulePath &&
    left.sessionName === right.sessionName &&
    equalToken(left.ipcToken, right.ipcToken);
}

function runtimeFields() {
  return {
    processPid: Number.isSafeInteger(process.pid) ? process.pid : null,
    nodeVersion: typeof process.versions?.node === "string" ? process.versions.node : null,
    electronVersion: typeof process.versions?.electron === "string" ? process.versions.electron : null,
  };
}

async function bootstrap(inputConfig) {
  const config = validateConfig(inputConfig);
  const reportTarget = config.reportPath ? `${config.reportPath}.agent.json` : null;
  const active = globalThis[STATE_KEY];
  if (active?.active) {
    if (sameRun(active.config, config)) {
      if (active.controller) return active.controller;
      throw codedError("bootstrap_initializing");
    }
    throw codedError("duplicate_bootstrap_active");
  }
  const registry = { active: true, initializing: true, config: { ...config }, controller: null };
  globalThis[STATE_KEY] = registry;

  const bootstrapStatus = {
    bootstrapLoaded: true,
    wrapperFound: null,
    wrapperSource: null,
    wrapperHashPinned: false,
    listenerRemovalMode: "unverified",
    sessionFound: null,
    serviceAvailable: null,
    listenerRegistered: false,
    connectionGeneration: config.connectionGeneration,
    ...runtimeFields(),
    lastFailureCode: null,
  };
  let wrapper = null;
  let agent = null;
  let activeController = null;
  let cleanupPromise = null;
  let disconnected = false;
  let intentionalClose = false;
  let rendererCapabilityProbeUsed = false;
  let asarSourceProbeUsed = false;
  let asarCandidateProbeUsed = false;
  let officialWindowProbeUsed = false;
  let rendererScriptMetadataProbeUsed = false;
  let officialAVObserverUsed = false;
  let officialAVObserver = null;
  let officialPrivateInvites = null;
  let bootstrapInitializationComplete = false;

  function releaseRegistry() {
    registry.active = false;
    if (globalThis[STATE_KEY] === registry) delete globalThis[STATE_KEY];
  }

  async function persistStatus() {
    if (!reportTarget) return;
    const directory = path.dirname(reportTarget);
    const temporary = `${reportTarget}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
    try {
      await fs.promises.mkdir(directory, { recursive: true });
      await fs.promises.writeFile(temporary, `${JSON.stringify(status(), null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
      await fs.promises.rename(temporary, reportTarget);
    } catch {
      bootstrapStatus.lastFailureCode = bootstrapStatus.lastFailureCode || "status_report_write_failed";
      try { await fs.promises.rm(temporary, { force: true }); } catch { /* best effort cleanup */ }
    }
  }

  function syncAgentStatus(result) {
    const current = result?.status || agent?.status?.();
    if (current) {
      bootstrapStatus.sessionFound = current.sessionFound;
      bootstrapStatus.serviceAvailable = current.serviceAvailable;
      bootstrapStatus.listenerRegistered = current.listenerRegistered;
      bootstrapStatus.lastFailureCode = current.lastFailureCode || null;
    }
  }

  function status() {
    return {
      ...bootstrapStatus,
      agent: agent ? agent.status() : null,
      officialPrivateInvites: officialPrivateInvites?.status() || null,
      ipc: activeController?.client.status() || { connected: false, handshaken: false, connectionGeneration: config.connectionGeneration },
    };
  }

  function transportAvailable() {
    const ipc = client.status();
    return !disconnected && ipc.connected && ipc.handshaken;
  }

  async function handleDisconnect(code) {
    disconnected = true;
    try { await officialAVObserver?.stop(); } catch { /* disconnect cleanup is bounded and diagnostic-only */ }
    try { await officialPrivateInvites?.stop(); } catch { /* the renderer's short lease remains the fallback */ }
    if (!intentionalClose) bootstrapStatus.lastFailureCode = code || "pipe_disconnected";
    if (!agent) {
      await persistStatus();
      return { ok: true, listenerRegistered: false };
    }
    if (cleanupPromise) return cleanupPromise;
    cleanupPromise = (async () => {
      await persistStatus();
      const result = await agent.detach();
      syncAgentStatus(result);
      if (!result.ok) {
        bootstrapStatus.lastFailureCode = result.code || "disconnect_detach_failed";
        await persistStatus();
        cleanupPromise = null;
        return result;
      }
      if (!intentionalClose) bootstrapStatus.lastFailureCode = code || "pipe_disconnected";
      await persistStatus();
      releaseRegistry();
      return result;
    })();
    return cleanupPromise;
  }

  const client = createPipeClient({
    pipeName: config.pipeName,
    connectionGeneration: config.connectionGeneration,
    ipcToken: config.ipcToken,
    onProbeRendererCapabilities() {
      const ipc = client.status();
      if (rendererCapabilityProbeUsed) throw codedError("renderer_probe_already_dispatched");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("renderer_probe_not_ready");
      }
      rendererCapabilityProbeUsed = true;
      return probeRendererCapabilities();
    },
    onProbeAsarSources() {
      const ipc = client.status();
      if (asarSourceProbeUsed) throw codedError("asar_source_probe_already_dispatched");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("asar_source_probe_not_ready");
      }
      asarSourceProbeUsed = true;
      return probeAsarSources({ appPath: config.appPath });
    },
    onProbeAsarCandidates() {
      const ipc = client.status();
      if (asarCandidateProbeUsed) throw codedError("asar_candidate_probe_already_dispatched");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("asar_candidate_probe_not_ready");
      }
      asarCandidateProbeUsed = true;
      return probeAsarCandidates({ appPath: config.appPath });
    },
    onProbeOfficialWindows() {
      const ipc = client.status();
      if (officialWindowProbeUsed) throw codedError("official_window_probe_already_dispatched");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("official_window_probe_not_ready");
      }
      officialWindowProbeUsed = true;
      return probeOfficialWindows();
    },
    onProbeRendererScriptMetadata() {
      const ipc = client.status();
      if (rendererScriptMetadataProbeUsed) throw codedError("renderer_script_metadata_probe_already_dispatched");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("renderer_script_metadata_probe_not_ready");
      }
      rendererScriptMetadataProbeUsed = true;
      return probeRendererScriptMetadata();
    },
    async onPrivateInviteLifecycle(body) {
      if (!transportAvailable() || !bootstrapInitializationComplete || !officialPrivateInvites?.status().available) {
        return { confirmed: false, code: "official_private_invite_unavailable" };
      }
      const result = await officialPrivateInvites.lifecycle(body);
      await persistStatus();
      return result;
    },
    onStartOfficialAVObserver(startOptions = {}) {
      let incomingOnly = false;
      let nativeConditional = false;
      try {
        if (!startOptions || typeof startOptions !== "object" || Array.isArray(startOptions) ||
            Object.getPrototypeOf(startOptions) !== Object.prototype) throw codedError("invalid_official_av_observer_start_options");
        const keys = Reflect.ownKeys(startOptions);
        if (keys.length === 1 && keys[0] === "incomingOnly") {
          const descriptor = Object.getOwnPropertyDescriptor(startOptions, "incomingOnly");
          if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.value !== true) {
            throw codedError("invalid_official_av_observer_start_options");
          }
          incomingOnly = true;
        } else if (keys.length === 1 && keys[0] === "nativeConditional") {
          const descriptor = Object.getOwnPropertyDescriptor(startOptions, "nativeConditional");
          if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.value !== true) {
            throw codedError("invalid_official_av_observer_start_options");
          }
          nativeConditional = true;
        } else if (keys.length !== 0) throw codedError("invalid_official_av_observer_start_options");
      } catch (error) {
        if (error?.code === "invalid_official_av_observer_start_options") throw error;
        throw codedError("invalid_official_av_observer_start_options");
      }
      const ipc = client.status();
      if (officialAVObserverUsed) throw codedError("official_av_observer_already_started");
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("official_av_observer_not_ready");
      }
      officialAVObserverUsed = true;
      officialAVObserver = createOfficialAVObserver({ incomingOnly, nativeConditional });
      return officialAVObserver.start();
    },
    onDrainOfficialAVObserver() {
      const ipc = client.status();
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("official_av_observer_not_ready");
      }
      if (!officialAVObserver) throw codedError("official_av_observer_not_started");
      return officialAVObserver.drain();
    },
    onStopOfficialAVObserver() {
      const ipc = client.status();
      if (!ipc.connected || !ipc.handshaken || !bootstrapInitializationComplete || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== config.connectionGeneration) {
        throw codedError("official_av_observer_not_ready");
      }
      if (!officialAVObserver) throw codedError("official_av_observer_not_started");
      return officialAVObserver.stop();
    },
    onEvent(op, payload) {
      if (op === "bootstrap_status" && payload && typeof payload === "object") {
        for (const key of ["bootstrapLoaded", "wrapperFound", "wrapperSource", "wrapperHashPinned", "listenerRemovalMode", "sessionFound", "serviceAvailable", "listenerRegistered", "connectionGeneration", "processPid", "nodeVersion", "electronVersion", "lastFailureCode"]) {
          if (Object.prototype.hasOwnProperty.call(payload, key)) bootstrapStatus[key] = payload[key];
        }
        void persistStatus();
      }
    },
    onAgentStatus(_operation, result) {
      syncAgentStatus(result);
      void persistStatus();
    },
  });
  // Install cleanup before connect/probe. createPipeClient also replays an
  // already-observed disconnect to late handlers, so no close event is lost.
  client.onDisconnect(handleDisconnect);
  try { await client.connect(); }
  catch (error) {
    releaseRegistry();
    throw error;
  }
  if (!transportAvailable()) {
    await handleDisconnect("pipe_disconnected");
    releaseRegistry();
    throw codedError("pipe_disconnected");
  }

  try {
    const loaded = loadWrapper(config);
    wrapper = loaded.wrapper;
    const removalContract = selectRemovalContract(config, loaded.modulePath);
    bootstrapStatus.wrapperFound = true;
    bootstrapStatus.wrapperSource = loaded.source;
    bootstrapStatus.wrapperHashPinned = removalContract.verified;
    bootstrapStatus.listenerRemovalMode = removalContract.mode;
    officialPrivateInvites = createOfficialPrivateInviteController({ isBindingCurrent(evidence) {
      const current = agent?.status();
      return transportAvailable() && bootstrapInitializationComplete && current?.listenerRegistered === true &&
        current.listenerRegistrationSequence === evidence.listenerRegistrationSequence;
    } });
    officialPrivateInvites.start();
    agent = createKernelAgent({
      wrapper,
      sessionName: config.sessionName,
      identity: config.identity,
      connectionGeneration: config.connectionGeneration,
      removalMode: removalContract.mode,
      nativeInviteScopeProfile: removalContract.nativeInviteScopeProfile,
      emit: (event) => {
        if (event?.method?.toLowerCase() === "oninviteactiontoavsdk") officialPrivateInvites.nativeInvite(event.nativeInviteEvidence || null);
        return client.publishEvent("kernel_event", event);
      },
    });
    const readiness = await agent.probe();
    syncAgentStatus(readiness);
    if (!readiness.ok) bootstrapStatus.lastFailureCode = readiness.code || "capability_probe_failed";
  } catch (error) {
    bootstrapStatus.wrapperFound = false;
    bootstrapStatus.lastFailureCode = error?.code || "wrapper_load_failed";
  }
  // Authenticated requests can arrive while the normal wrapper probe is still
  // in progress. The private capability RPC is available only after this
  // existing initialization attempt has settled, regardless of its outcome.
  bootstrapInitializationComplete = true;

  if (!transportAvailable()) {
    if (agent && cleanupPromise) await cleanupPromise;
    if (agent && agent.status().listenerRegistered) await agent.detach();
    bootstrapStatus.lastFailureCode = "pipe_disconnected";
    releaseRegistry();
    await persistStatus();
    throw codedError("pipe_disconnected");
  }
  // Keep the Agent unavailable to authenticated requests until its capability
  // probe has settled. Startup remains capability-only; attach is still explicit.
  if (agent) client.setAgent(agent);

  client.setBootstrapStatus(bootstrapStatus);
  try { client.publishEvent("bootstrap_status", { ...bootstrapStatus }); }
  catch { bootstrapStatus.lastFailureCode = "bootstrap_status_publish_failed"; }
  await persistStatus();

  // persistStatus yields to the filesystem. A disconnect in that window must
  // not let bootstrap return a controller whose Agent can register afterward.
  if (!transportAvailable()) {
    if (cleanupPromise) await cleanupPromise;
    if (agent && agent.status().listenerRegistered) await agent.detach();
    bootstrapStatus.lastFailureCode = "pipe_disconnected";
    releaseRegistry();
    await persistStatus();
    throw codedError("pipe_disconnected");
  }

  const controller = Object.freeze({
    status,
    async attach() {
      if (!transportAvailable()) return { ok: false, code: "agent_disconnected", status: status() };
      if (!agent) return { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable", status: status() };
      const result = await agent.attach();
      syncAgentStatus(result);
      client.setBootstrapStatus(bootstrapStatus);
      try { client.publishEvent("bootstrap_status", { ...bootstrapStatus }); } catch { /* disconnection cleanup follows */ }
      await persistStatus();
      return result;
    },
    async forwardAVSDKOutput(output) {
      if (!transportAvailable()) return { ok: false, code: "agent_disconnected", status: status() };
      if (!agent) return { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable", status: status() };
      const result = await agent.forwardAVSDKOutput({ ...output, generation: config.connectionGeneration });
      syncAgentStatus(result);
      client.setBootstrapStatus(bootstrapStatus);
      await persistStatus();
      return result;
    },
    async getAccountPath({ uin } = {}) {
      if (!transportAvailable()) return { ok: false, code: "agent_disconnected" };
      if (!agent) return { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable" };
      return agent.getAccountPath({ uin, generation: config.connectionGeneration });
    },
    async detach() {
      if (!agent) return { ok: true, status: status() };
      const result = await agent.detach();
      syncAgentStatus(result);
      client.setBootstrapStatus(bootstrapStatus);
      try { client.publishEvent("bootstrap_status", { ...bootstrapStatus }); } catch { /* disconnected */ }
      await persistStatus();
      return result;
    },
    async close() {
      if (cleanupPromise) return cleanupPromise;
      cleanupPromise = (async () => {
        const result = agent ? await agent.detach() : { ok: true, status: null };
        syncAgentStatus(result);
        if (result.ok !== true || bootstrapStatus.listenerRegistered !== false) {
          bootstrapStatus.lastFailureCode = result.code || "detach_unconfirmed";
          await persistStatus();
          cleanupPromise = null;
          return { ok: false, code: bootstrapStatus.lastFailureCode, status: status() };
        }
        intentionalClose = true;
        await client.close();
        registry.active = false;
        if (globalThis[STATE_KEY] === registry) delete globalThis[STATE_KEY];
        await persistStatus();
        return { ok: true, status: status() };
      })();
      return cleanupPromise;
    },
    client,
  });
  activeController = controller;
  registry.initializing = false;
  registry.controller = controller;

  // The previous report was written before activeController existed, so its
  // IPC field may contain the disconnected fallback. Persist a final snapshot
  // after binding the authenticated controller and run registry.
  await persistStatus();
  if (!transportAvailable()) {
    if (cleanupPromise) await cleanupPromise;
    if (agent && agent.status().listenerRegistered) await agent.detach();
    bootstrapStatus.lastFailureCode = "pipe_disconnected";
    releaseRegistry();
    await persistStatus();
    throw codedError("pipe_disconnected");
  }

  return controller;
}

module.exports = bootstrap;
module.exports.bootstrap = bootstrap;
module.exports.loadWrapper = loadWrapper;
module.exports.validateConfig = validateConfig;
module.exports.removalModeForDigest = removalModeForDigest;
module.exports.selectRemovalContract = selectRemovalContract;
