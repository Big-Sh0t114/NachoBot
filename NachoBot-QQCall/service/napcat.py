"""Discover existing local OneBot servers for one configured NapCat account."""

import json
import re
from pathlib import Path

from .private_files import is_reparse_point


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("adapter_endpoint_mismatch")
        value[key] = item
    return value


def configured_onebot_connections(config: dict) -> list[tuple[str, str]]:
    """Return credentials privately; callers must still prove live process/account ownership."""
    try:
        account = config["account_id"]
        if not isinstance(account, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", account):
            raise ValueError
        root = Path(config["paths"]["loader_root"])
        target = root / "config" / f"onebot11_{account}.json"
        if any(is_reparse_point(path) for path in (root, target.parent, target)):
            raise ValueError
        target.resolve(strict=True).relative_to(root.resolve(strict=True))
        with target.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        network = document["network"]
        choices = []
        for key, scheme in (("websocketServers", "ws"), ("httpServers", "http")):
            servers = network.get(key, [])
            if not isinstance(servers, list) or len(servers) > 32:
                raise ValueError
            for server in servers:
                if not isinstance(server, dict) or server.get("enable") is not True:
                    continue
                host, port, token = server.get("host"), server.get("port"), server.get("token", "")
                if (host not in ("127.0.0.1", "::1", "[::1]") or type(port) is not int
                        or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) > 4096
                        or any(ord(c) < 32 for c in token)):
                    continue
                host = "[::1]" if host in ("::1", "[::1]") else host
                choices.append((f"{scheme}://{host}:{port}/", token))
        preferred = config["onebot"]["url"].rstrip("/")
        choices.sort(key=lambda item: (item[0].rstrip("/") != preferred, not bool(item[1])))
        return list(dict.fromkeys(choices))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("adapter_endpoint_mismatch") from None
