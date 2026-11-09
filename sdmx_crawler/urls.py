"""Pembangun URL SDMX 3.0 yang deterministik (dipakai sebagai kunci unik)."""
from __future__ import annotations

from urllib.parse import quote, urlencode

# Karakter yang bermakna di SDMX 3.0 dan tidak boleh di-escape:
# '*' (wildcard), '+' (versi terbaru / operator AND), ':' (operator filter), '[' ']' (c[DIM]).
_PATH_SAFE = "*+.,()-_"
_QUERY_SAFE = ":,+*[]()-._"


def build_url(base: str, segments: list[str], params: dict | None = None) -> str:
    path = "/".join(quote(str(s), safe=_PATH_SAFE) for s in segments)
    url = f"{base.rstrip('/')}/{path}"
    if params:
        url += "?" + urlencode(sorted(params.items()), safe=_QUERY_SAFE, quote_via=quote)
    return url
