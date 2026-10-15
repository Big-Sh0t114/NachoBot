"use strict";

const path = require("node:path");
const { buildHostConfigs, codedError, writePrivateJson } = require("../host/config.cjs");

function parseArgs(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--help" || key === "-h") return { help: true, values };
    if (!key.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) {
      throw codedError("invalid_arguments");
    }
    if (values.has(key)) throw codedError("duplicate_argument");
    values.set(key, argv[index + 1]);
    index += 1;
  }
  return { help: false, values };
}

function required(values, key) {
  const value = values.get(key);
  if (typeof value !== "string" || !value) throw codedError(`missing_${key.slice(2).replace(/-/g, "_")}`);
  return value;
}

function positiveInteger(value, code) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) throw codedError(code);
  return parsed;
}

function absolutePathOption(value, code) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw codedError(code);
  return path.resolve(value);
}

function buildAndWrite(values) {
  const targetPid = positiveInteger(required(values, "--pid"), "host_pid_invalid");
  const appPath = absolutePathOption(required(values, "--app-path"), "app_path_invalid");
  const reportPath = absolutePathOption(required(values, "--report"), "report_path_invalid");
  const profilePath = absolutePathOption(required(values, "--profile-path"), "profile_path_invalid");
  const listenPort = positiveInteger(required(values, "--listen-port"), "listen_port_invalid");
  const outputPort = positiveInteger(required(values, "--output-port"), "output_port_invalid");
  const hostGeneration = positiveInteger(required(values, "--generation"), "host_generation_invalid");
  const sessionName = values.get("--session") || "qqcall_av_host";
  const configPath = absolutePathOption(
    values.get("--config-out") || `${reportPath}.host.bootstrap.config.json`,
    "config_output_path_invalid",
  );
  const sidecarPath = `${reportPath}.host.config.json`;
  if (path.resolve(configPath).toLowerCase() === path.resolve(sidecarPath).toLowerCase()) {
    throw codedError("config_paths_must_differ");
  }

  const { nativeConfig, hostConfig } = buildHostConfigs({
    targetPid,
    appPath,
    reportPath,
    profilePath,
    listenPort,
    outputPort,
    hostGeneration,
    sessionName,
  });
  writePrivateJson(configPath, nativeConfig);
  writePrivateJson(sidecarPath, hostConfig);
  return Object.freeze({ targetPid, hostGeneration, configPath, sidecarPath });
}

function usage() {
  return [
    "Build the private Host sidecar and native-compatible QQCall Shim config.",
    "",
    "Required: --pid <host QQ PID> --app-path <resolved QQ --app-path>",
    "          --report <absolute report base> --profile-path <dedicated userData directory>",
    "          --listen-port <1..65535> --output-port <1..65535> --generation <positive integer>",
    "Optional: --session <name> --config-out <absolute native config path>",
    "",
    "This command only writes private config files; it does not launch or inject a process.",
  ].join("\n");
}

function main(argv = process.argv.slice(2)) {
  try {
    const parsed = parseArgs(argv);
    if (parsed.help) {
      process.stdout.write(`${usage()}\n`);
      return 0;
    }
    const result = buildAndWrite(parsed.values);
    process.stdout.write(`${JSON.stringify({ ok: true, ...result })}\n`);
    return 0;
  } catch (error) {
    const code = typeof error?.code === "string" && /^[a-z][a-z0-9_]{1,63}$/.test(error.code)
      ? error.code
      : "host_config_failed";
    process.stderr.write(`${JSON.stringify({ ok: false, code })}\n`);
    return 1;
  }
}

if (require.main === module) process.exitCode = main();

module.exports = { buildAndWrite, main, parseArgs, usage };
