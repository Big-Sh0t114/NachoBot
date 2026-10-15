# ASAR physical keeper (candidate diagnostic)

`probe/asar-physical-keeper.py` is an isolated, opt-in `--stdio` helper. It is
not imported by QQCall, the agent, bootstrap, broker, admission path, or the
existing source probe. Its result is candidate/unverified evidence only. It
does not read ASAR virtual entries and cannot make a source-probe result succeed.

The helper accepts exactly one initial UTF-8 JSON line with this schema:

```json
{"schema":"asar.physical.keeper.v1","nonce":"<64 hex characters>","kernelPid":1234,"kernelCreationTime100ns":"133700000000000000","connectionGeneration":1,"appPath":"C:\\Program Files\\Tencent\\QQNT\\versions\\9.9.26-44343\\resources\\app"}
```

The nonce binds the subsequent finish request. `appPath` must identify the
single fixed application directory above. The helper starts from the `C:\`
volume root and opens each fixed path component relative to the previous open
directory handle. Those opens use `NtCreateFile` with `RootDirectory`,
`OBJ_DONT_REPARSE`, `FILE_OPEN_REPARSE_POINT`, and directory/file type options.
Every opened component is checked for a reparse attribute, expected type,
canonical final path, volume serial, file ID, size, and last-write time. The
component-relative opens and held directory handles are a diagnostic guard;
they do not establish a complete namespace-fencing guarantee.

The supplied kernel PID and creation FILETIME must match the held process
handle, and its image must resolve case-insensitively to the fixed
`C:\Program Files\Tencent\QQNT\QQ.exe` path. The helper also tracks its own
process image and creation FILETIME through finish.

The helper opens only `application.asar`, `package.json`, and `wrapper.node`
under that fixed directory. It uses the hashes already pinned by
`agent/asar-source-probe.cjs`, with 64 MiB, 64 KiB, and 128 MiB respective
size limits. File handles are read-only and share reads. It hashes each whole
file in chunks no larger than 1 MiB, checks metadata before and after each
digest, and retains the same file and ancestor handles until the finish request
is validated. It does not create, write, rename, or delete files.

After all initial checks pass, the helper writes one `ready` JSON line. The
caller must then send exactly `{"op":"finish","nonce":"<same nonce>"}` as
one bounded line. The helper rechecks the same held process handles and the
same already-open file handles, including each full pinned hash and native
identity. It then attempts to close the complete resource stack. It writes a
`final` line and exits zero only if all closes succeed. It never reopens an
object during finish. If a close fails, it attempts every remaining close,
writes a fixed `resource_close_failed` rejection, and exits nonzero.

Each input line is at most 4096 bytes, each output line is at most 64 KiB, and
the entire helper session has a 15-second monotonic deadline. Rejections are
single-line JSON objects with exactly `schema`, `state`, and `code`; they never
contain native exception text, paths, or metadata values. The fixed rejection
codes are:

```text
input_too_large, input_eof, input_timeout, input_read_failed, input_invalid,
input_schema_rejected, finish_schema_rejected, unknown_operation, nonce_invalid,
nonce_mismatch, binding_invalid, app_path_rejected, process_query_unsupported,
process_identity_mismatch, process_identity_changed, native_query_unsupported,
component_open_failed, component_reparse_rejected, component_type_rejected,
canonical_path_mismatch, file_type_rejected, file_size_invalid,
file_size_exceeded, file_metadata_changed, file_hash_unavailable,
file_hash_mismatch, output_too_large, output_write_failed,
runtime_deadline_exceeded, resource_close_failed, internal_error
```

The `ready` and `final` file records contain a canonical final path, native
volume serial and file ID, file size, last-write time, and SHA-256 because the
frames are intended for a private parent/child pipe. They are not suitable for
logs or user-facing output. No successful namespace-proof claim should be
derived from these frames: the separate Windows fixture still lacks a verified
real-junction case, and a held file handle matching the pin does not prove that
the application's later pathname lookup resolves to that file. The legacy
archive `dev` check remains unchanged.

The `--stdio` entrypoint reads through `sys.stdin.buffer.raw` so a timed-out
read cannot leave CPython's buffered-reader lock held during shutdown. The
protocol still reads one bounded newline-delimited frame at a time.

## Source syntax check

From `NachoBot-QQCall`, run:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& .\.venv\Scripts\python.exe -B -c "import pathlib, py_compile, tempfile; p=pathlib.Path('probe/asar-physical-keeper.py'); f=tempfile.NamedTemporaryFile(suffix='.pyc', delete=False); f.close(); py_compile.compile(str(p), cfile=f.name, doraise=True); pathlib.Path(f.name).unlink()"
```

This syntax check does not invoke the real `WindowsPhysicalNative`, open the
installed QQ tree, inspect a QQ process, or establish Windows sharing/reparse
behavior. Runtime acceptance remains a separate target-environment check.
