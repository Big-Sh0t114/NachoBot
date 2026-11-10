# Shared bounded local file reads

`src.common.file_read` provides two standard-library helpers for callers that need a
strict local-file byte budget:

- `read_bounded_file(path, max_bytes=...)` validates a positive integer budget, opens
  the path once in binary mode, reads at most `max_bytes + 1` bytes, and raises
  `BoundedFileTooLargeError` when that extra byte exists.
- `read_bounded_text_file(path, max_bytes=..., encoding="utf-8")` applies the same
  byte budget before strict decoding, then converts CRLF and lone CR newlines to LF.

These helpers do not use file metadata as a size guarantee. A file that grows after a
caller checks its path still cannot make the read exceed the requested allocation by
more than the one-byte overflow probe. The upload caller maps the helper's overflow
exception to its existing 1 MB user-facing `ValueError`; text previews retain their
existing silent skip behavior; video reads retain the existing URL fallback when a
local file is too large.

The helpers do not add a path, symlink, file-type, or cache policy. Callers continue to
own those decisions and their existing error handling.
