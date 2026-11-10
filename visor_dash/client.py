"""Read-only client for the Vauban VISOR+2 / VERSO+ embedded web server.

Formats are taken from the controller's own UI script (/assets/Outils.js).
This module can only issue GET requests to an explicit allowlist of paths:
it has no way to modify users, open doors or flash firmware.
"""
from __future__ import annotations

import gzip
import re
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

# Paths the client is allowed to fetch. Anything else raises.
READ_ONLY_PATHS = {
    "/assets/Users",
    "/assets/Groupes",
    "/assets/Lecteurs",
    "/assets/Lifts",
    "/assets/Plages",
    "/FR/GetEvenements.cgi",
    "/FR/index.cgi",
    "/FR/utilisateurs-creation.cgi",   # user form: read-only page
    "/FR/horaires-groupe.cgi",         # group form: read-only page
    "/FR/horaires-plage.cgi",          # time-slot form: read-only page
    "/FR/horaires-jours-feries.cgi",   # public holidays list
}
# Forms that may be posted to update.cgi: users (11), groups (2), time slots (4).
_POSTABLE_FORMS = (b"type=11&id=", b"type=2&id=", b"type=4&id=")
USER_UPDATE_PATH = "/FR/update.cgi"
USER_DELETE_PATH = "/FR/delete.cgi"
DOOR_OPEN_PATH = "/FR/open.cgi"
_DELETE_USER_QUERY = re.compile(r"^type=11&index=[1-9][0-9]{0,4}$")
TRANSLATION_PATH = "/assets/traductions-{lang}.js"
LANGS = ("fr", "en", "it")

USER_RECORD_SIZE = 49
DELETED_USER_ID = 65535

IDENTIFIER_TYPES = {1: "strBadgeProximite", 2: "strBadgeApplication", 3: "strEmetteur", 4: "strPincode"}

# Event categories, used for colour coding and filters.
GRANTED = {10, 27, 33, 49, 54}
REFUSED = (set(range(0, 7)) | set(range(12, 36))) - GRANTED - {25, 32}  # 25/32 = waiting states
ALARM = {40, 41, 53, 55, 58, 59, 61, 63, 67, 72, 74, 78, 100, 101, 103, 200}


def event_category(code: int) -> str:
    if code in GRANTED:
        return "granted"
    if code in REFUSED:
        return "refused"
    if code in ALARM:
        return "alarm"
    return "info"


# --------------------------------------------------------------------------- parsers

@dataclass
class User:
    index: int            # slot number, 1-based; events refer to users by this
    identifier: int       # badge / code number
    id_type: int          # 1 proximity badge, 2 app badge, 3 remote, 4 PIN, 9 = no identifier shown by Vauban UI
    groups: Tuple[int, int, int]
    last_name: str
    first_name: str
    status: int           # 0 authorised, 1 forbidden


def _cstr(raw: bytes) -> str:
    # Exact stored text (some names carry a trailing space); trim only for display.
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def parse_users(data: bytes) -> List[User]:
    """/assets/Users: one 49-byte record per slot; slot n is user index n."""
    users = []
    for slot, off in enumerate(range(0, len(data) - USER_RECORD_SIZE + 1, USER_RECORD_SIZE), start=1):
        rec = data[off:off + USER_RECORD_SIZE]
        id_type = rec[7]
        if id_type == 0:
            continue  # empty slot
        users.append(User(
            index=slot,
            identifier=int.from_bytes(rec[0:7], "little"),
            id_type=id_type,
            groups=(
                int.from_bytes(rec[8:10], "little"),
                int.from_bytes(rec[10:12], "little"),
                int.from_bytes(rec[12:14], "little"),
            ),
            last_name=_cstr(rec[14:31]),
            first_name=_cstr(rec[31:48]),
            status=rec[48],
        ))
    return users


def parse_label_list(data: bytes, default_prefix: str) -> Dict[int, str]:
    """Groupes / Lecteurs / Lifts / Plages: entries separated by '#', CR or LF.

    Mirrors ChargeTableau1Dimensions(): entry i (1-based) gets its label, or
    "<default_prefix> i" when the label is only NUL padding.
    """
    text = data.decode("utf-8", errors="replace")
    for sep in ("\r", "\n"):
        text = text.replace(sep, "#")
    labels = {}
    for i, raw in enumerate(text.split("#"), start=1):
        if not raw:
            continue
        label = raw.replace("\0", "").strip()
        labels[i] = label or f"{default_prefix} {i}".strip()
    return labels


def parse_translations(data: bytes) -> Dict[str, str]:
    text = data.decode("utf-8-sig", errors="replace")
    out = {}
    for line in text.splitlines():
        parts = line.split("=")
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


@dataclass
class RawEvent:
    date: str
    code: int
    reader: int
    identifier: str
    user_index: int


def parse_events(data: bytes) -> Tuple[List[RawEvent], Optional[int]]:
    """GetEvenements.cgi: 'date$code$reader$identifier$user#...#<next index>#'.

    Returns the events and the next page index (None when there are no more).
    """
    text = data.decode("utf-8", errors="replace")
    for sep in ("\r", "\n"):
        text = text.replace(sep, "#")
    events: List[RawEvent] = []
    next_index: Optional[int] = None
    for line in text.split("#"):
        fields = line.split("$")
        if len(fields) > 1:
            fields += [""] * (5 - len(fields))
            events.append(RawEvent(
                date=fields[0],
                code=_int(fields[1]),
                reader=_int(fields[2]),
                identifier=fields[3],
                user_index=_int(fields[4]),
            ))
        elif fields[0].strip():
            n = _int(fields[0])
            next_index = n if n >= 0 else None
            break
    return events, next_index


def _int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- HTTP

class ControllerError(Exception):
    pass


class VaubanClient:
    """GET-only access to the controller, Digest-authenticated, one request at a time."""

    def __init__(self, host: str, user: str, password: str, port: int = 80, timeout: float = 20.0,
                 allow_writes: bool = False, allow_door_open: bool = False):
        self.allow_writes = allow_writes
        self.allow_door_open = allow_door_open
        self.base = f"http://{host}:{port}" if port != 80 else f"http://{host}"
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.base + "/", user, password)
        self._opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        self._timeout = timeout
        # The controller is a small embedded device: never send it parallel requests.
        self._lock = threading.Lock()

    def get(self, path: str, query: str = "") -> bytes:
        allowed = path in READ_ONLY_PATHS or path in {TRANSLATION_PATH.format(lang=l) for l in LANGS}
        if not allowed:
            raise PermissionError(f"read-only client refuses path {path!r}")
        return self._request(path, query)

    def download_backup(self) -> bytes:
        """The controller's own "Sauvegarder": build the backup, then download /BACKUP.

        Not part of get(): backup.cgi makes the controller write a file, so it
        only runs through this method.
        """
        reply = self._request("/FR/backup.cgi").decode("utf-8", errors="replace")
        if "Succes()" not in reply.split("</script>")[0].rsplit("}", 1)[-1]:
            raise ControllerError("the controller did not confirm the backup")
        data = self._request("/BACKUP")
        if not data.startswith(b"CENTRALE") or len(data) < 1024:
            raise ControllerError("the downloaded backup does not look like a Vauban backup file")
        return data

    # Writes: only the two user-management requests, and only when enabled.
    def post_form(self, path: str, body: bytes) -> bytes:
        if not self.allow_writes:
            raise PermissionError("writes are disabled (set DASH_ALLOW_WRITES=1)")
        if path != USER_UPDATE_PATH or not body.startswith(_POSTABLE_FORMS):
            raise PermissionError("only the user, group and time-slot forms may be posted")
        return self._request(path, "", body)

    def open_door(self, reader: int) -> bytes:
        """Momentary unlock of one door, like the "Open" button of Vauban's UI.
        Separate switch from user editing: it acts on the building, not on data."""
        if not self.allow_door_open:
            raise PermissionError("door opening is disabled (set DASH_ALLOW_DOOR_OPEN=1)")
        if isinstance(reader, bool) or not isinstance(reader, int) or not 1 <= reader <= 127:
            raise PermissionError("invalid door number")
        return self._request(DOOR_OPEN_PATH, f"index={reader}")

    def get_action(self, path: str, query: str) -> bytes:
        if not self.allow_writes:
            raise PermissionError("writes are disabled (set DASH_ALLOW_WRITES=1)")
        if path != USER_DELETE_PATH or not _DELETE_USER_QUERY.match(query):
            raise PermissionError("only single-user deletion is allowed")
        return self._request(path, query)

    def _request(self, path: str, query: str = "", body: Optional[bytes] = None) -> bytes:
        url = self.base + path + (f"?{query}" if query else "")
        req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self._lock:
            try:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as e:
                raise ControllerError(f"{path}: HTTP {e.code}") from e
            except (urllib.error.URLError, OSError) as e:
                raise ControllerError(f"{path}: {getattr(e, 'reason', e)}") from e
        # Static assets (.js/.css) are stored pre-gzipped on the controller.
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return body

    # Convenience wrappers ------------------------------------------------
    def users(self) -> List[User]:
        return parse_users(self.get("/assets/Users"))

    def labels(self, name: str, default_prefix: str) -> Dict[int, str]:
        return parse_label_list(self.get(f"/assets/{name}"), default_prefix)

    def translations(self, lang: str = "fr") -> Dict[str, str]:
        if lang not in LANGS:
            lang = "fr"
        return parse_translations(self.get(TRANSLATION_PATH.format(lang=lang)))

    def events(self, index: int = 0) -> Tuple[List[RawEvent], Optional[int]]:
        return parse_events(self.get("/FR/GetEvenements.cgi", f"index={int(index)}"))

    def versions(self) -> Dict[str, str]:
        """Web server and controller versions from index.cgi ('35' -> '3.5')."""
        html = self.get("/FR/index.cgi").decode("utf-8", errors="replace")
        out = {}
        for key, name in (("version", "web_server"), ("versionCentrale", "controller")):
            m = re.search(rf"\b{key}\s*=\s*'(\d+)'", html)
            if m and len(m.group(1)) >= 2:
                v = m.group(1)
                out[name] = f"{v[0]}.{v[1:]}"
        return out


def user_to_dict(u: User) -> dict:
    d = asdict(u)
    d["groups"] = list(u.groups)
    return d
