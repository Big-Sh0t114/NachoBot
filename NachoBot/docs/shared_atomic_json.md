# Shared atomic JSON persistence

`src.common.atomic_json.atomic_write_json(path, value, *, ensure_ascii=False, indent=None)`
is the shared writer for Core JSON files. It keeps the regular `json.dump`
formatting behavior, streams the value to a uniquely named temporary file in the
resolved destination directory, flushes and fsyncs that file, closes it, then
commits with `os.replace`. Serialization, encoding, write, fsync, close, and
replace errors propagate to the caller. The old destination is unchanged for
failures before the replace succeeds, and a failed new write leaves no target.

The destination is resolved before creating its parent directory, so a live or
dangling destination symlink is followed as with the prior `open(..., "w")`
behavior. The resolution is not a security boundary and does not promise
protection from adversarial path races. Temporary files are unique per call and
only that call's temp path is eligible for cleanup. Cleanup is best-effort; an
OS-level denial can leave a temporary file behind without replacing the primary
write error. If closing the raw descriptor reports an error, the writer does not
retry that descriptor number because the OS may already have released it for reuse;
the close error propagates and the temp path still receives best-effort cleanup.

On POSIX, replacing an existing regular file preserves its permission mode.
Replacement creates a new inode using directory permissions; it does not
preserve ownership, ACLs, hard-link identity, or the old file's write-permission
semantics. New files retain `mkstemp`'s restrictive default mode. On Windows, a
read-only destination may make `os.replace` fail; its flags are not changed.

The guarantee is atomic file visibility on the same filesystem. It does not
include directory fsync, power-loss durability, a transaction or merge between
writers, or a guarantee about which concurrent writer wins. Independent writers
use separate temp files, and the last successful replace wins.

References: [Python `os.replace`](https://docs.python.org/3.12/library/os.html#os.replace)
and [Python `tempfile.mkstemp`](https://docs.python.org/3.12/library/tempfile.html#tempfile.mkstemp).
