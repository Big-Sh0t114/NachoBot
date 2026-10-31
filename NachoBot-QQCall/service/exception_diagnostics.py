"""Allowlist the Bridge's optional fatal-exception diagnostics."""

_TYPES = {"unknown", "Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "URIError", "EvalError", "AggregateError"}
_CODES = {"unknown", "EPIPE", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "ENOTCONN", "EBADF", "ENOMEM", "ERR_STREAM_DESTROYED", "ERR_STREAM_WRITE_AFTER_END", "ERR_UNHANDLED_ERROR"}
_FILES = {"bridge/cli.cjs", "bridge/host-client.cjs", "bridge/control-service.cjs", "bridge/qqcall-bridge.cjs", "bridge/audio-route-observer.cjs", "probe/broker.cjs"}


def validated_exception_details(value):
    if type(value) is not dict or set(value) != {"type", "code", "locations"}:
        return None
    if type(value["type"]) is not str or value["type"] not in _TYPES:
        return None
    if type(value["code"]) is not str or value["code"] not in _CODES:
        return None
    locations = value["locations"]
    if type(locations) is not list or len(locations) > 8:
        return None
    for location in locations:
        if type(location) is not dict or set(location) != {"file", "line", "column"}:
            return None
        if type(location["file"]) is not str or location["file"] not in _FILES:
            return None
        if any(type(location[key]) is not int or not 1 <= location[key] <= 9_999_999 for key in ("line", "column")):
            return None
    return {"type": value["type"], "code": value["code"], "locations": [dict(item) for item in locations]}
