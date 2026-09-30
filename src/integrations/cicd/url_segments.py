"""Every value a gateway puts into a URL path or a fixture file name, validated (SEC-03, SEC-24).

A file path, a commit sha or a branch name reaches the gateways from the model (a drafted
plan, an optional read call) or from a caller (a run's subject). Interpolated raw, `../`
leaves the repository -- httpx resolves dot segments before sending, so
`contents/../../../user` goes out as `GET /repos/user` with the token attached -- and a
`?` or `#` rewrites the query or drops the rest of the URL. The replay gateway turns the
same values into file names under a scenario directory.

Each function takes the raw value, refuses anything that is not a `str` (a missing
argument must not become the path `"None"`), and returns the value to use or raises
`ValueError`. Every pattern is matched whole (`fullmatch`): `$` alone also matches before
a trailing newline. Both gateways already turn a `ValueError` from argument handling into
`ToolError(kind="invalid_args")` before anything is sent, so a refused value costs no
request -- which matters in dry run too, where the pre-check `GET`s still carry the token.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import quote

#: A commit sha, abbreviated or full, lower-case hex as GitHub returns it.
_SHA: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,40}")
#: `owner/name` as GitHub spells them: letters, digits, `-`, `_` and `.`.
_REPO: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
#: Characters git's `check-ref-format` refuses anywhere in a ref name, with the ASCII
#: control characters and DEL. A space is one of them.
_REF_FORBIDDEN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
#: Characters refused in a file path: the URL's own delimiters and escape, a Windows
#: separator, and the control characters.
_PATH_FORBIDDEN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f\\?#%]")
_MAX_REF_CHARS: Final[int] = 255
_MAX_PATH_CHARS: Final[int] = 1024


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is missing or not a string")
    return value


def sha(value: object, *, name: str = "sha") -> str:
    text = _text(value, name)
    if not _SHA.fullmatch(text):
        raise ValueError(f"{name} is not a commit sha")
    return text


def repo_name(value: object, *, name: str = "repo") -> str:
    """`owner/name`, with no dot segment."""
    text = _text(value, name)
    if not _REPO.fullmatch(text) or ".." in text or any(
        part in (".", "..") for part in text.split("/")
    ):
        raise ValueError(f"{name} is not an owner/name repository")
    return text


def ref_name(value: object, *, name: str = "ref") -> str:
    """A branch name (or a sha, which is also a valid name) under git's ref-name rules."""
    text = _text(value, name)
    if not text or len(text) > _MAX_REF_CHARS:
        raise ValueError(f"{name} is empty or too long")
    if (
        _REF_FORBIDDEN.search(text)
        or ".." in text
        or "//" in text
        or "@{" in text
        or text == "@"
        or text.startswith("/")
        or text.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") or part.endswith(".lock") for part in text.split("/"))
    ):
        raise ValueError(f"{name} is not a valid branch or ref name")
    return text


def ref_path(value: object, *, name: str = "ref") -> str:
    """`ref_name`, percent-encoded one segment at a time for a URL path."""
    return "/".join(quote(part, safe="") for part in ref_name(value, name=name).split("/"))


def file_path(value: object, *, name: str = "path") -> str:
    """A repository-relative file path. A leading `/` is dropped, as before."""
    text = _text(value, name).lstrip("/")
    if not text or len(text) > _MAX_PATH_CHARS or _PATH_FORBIDDEN.search(text):
        raise ValueError(f"{name} is not a valid repository file path")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{name} is not a valid repository file path")
    return text


def file_url_path(value: object, *, name: str = "path") -> str:
    """`file_path`, percent-encoded one segment at a time for a URL path."""
    return "/".join(quote(part, safe="") for part in file_path(value, name=name).split("/"))
