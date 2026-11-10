"""User management for the Vauban controller: read, create, edit, delete one user.

Everything here mirrors what the controller's own UI does (Outils.js):

* The edit form is /FR/utilisateurs-creation.cgi. Its HTML gives the fields and
  their defaults; the controller then injects a few JavaScript statements with
  the user's current values. Only a closed set of statement shapes is accepted
  (found by surveying every user on the controller). Any other injected line
  makes the page "not understood" and the write is refused, so a field we do
  not know about can never be silently reset.
* Saving sends the form exactly like EnvoiPostAjax(): every control in document
  order, "name=value&", checkboxes as on/off, values NOT percent-encoded.
* Deleting is GET /FR/delete.cgi?type=11&index=<form id>.

Every write is checked afterwards against /assets/Users: the target slot must
hold what we sent and every other slot must be byte-for-byte unchanged.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from .client import USER_RECORD_SIZE, User, parse_users

FORM_PATH = "/FR/utilisateurs-creation.cgi"
UPDATE_PATH = "/FR/update.cgi"
DELETE_PATH = "/FR/delete.cgi"

# Field names expected in the user form, in document order. If the controller
# serves anything else (firmware change), writes are refused.
EXPECTED_FIELDS = [
    "type", "id", "empreinte", "ancienTypeIdentifiant", "lastname", "firstname", "ident",
    "typeIdentifiant", "presence", "flagapb", "razapb", "authstatus", "plage", "authdates",
    "authdate1", "authdate1hour", "authdate1min", "authdate2", "authdate2hour", "authdate2min",
    "groupeAcces1", "groupeAcces2", "groupeAcces3", "alarm", "nocontrol", "cycleback",
    "modeOffice", "codeBadge", "codeSaisi", "nbpass", "nbpassvalue", "flagnbpass",
    "passrestantsvalue", "listeModuleEnrolement",
]

ID_TYPES = {1: "Proximity badge", 2: "App badge", 3: "Remote", 4: "PIN code"}
NO_IDENTIFIER_TYPE = 9      # the controller's type for a user without badge
SLOT_TIME_FROM_GROUP = 255  # "plage" value: use the group's time slot
SLOT_ALWAYS = 0             # "plage" value: permanent access

NAME_MAX_BYTES = 16
NAME_RE = re.compile(r"^[0-9A-Za-zÀ-ÖØ-öø-ÿŒœ .'()_-]*$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


# One controller write at a time, whatever is being changed (people, groups, time slots).
WRITE_LOCK = threading.RLock()


class FormError(Exception):
    """The controller's page was not what we expect: refuse to write."""


class ValidationError(ValueError):
    """The requested change is not acceptable."""


class WriteFailed(Exception):
    """The controller answered, but the result could not be verified."""


# --------------------------------------------------------------------------- form model

@dataclass
class Control:
    name: str
    kind: str                      # hidden, text, number, checkbox, select
    value: str = ""
    checked: bool = False
    options: List[str] = field(default_factory=list)

    def serialized(self) -> str:
        if self.kind == "checkbox":
            return f"{self.name}={'on' if self.checked else 'off'}&"
        return f"{self.name}={self.select_value() if self.kind == 'select' else self.value}&"

    def select_value(self) -> str:
        if self.value in self.options:
            return self.value
        return self.options[0] if self.options else ""


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.controls: List[Control] = []
        self._select: Optional[Control] = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "formUtilisateur":
            self.in_form = True
        if not self.in_form:
            return
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("button", "submit", "reset", "image") or not a.get("name"):
                return  # EnvoiPostAjax skips unnamed controls
            self.controls.append(Control(a["name"], kind, a.get("value") or "", "checked" in a))
        elif tag == "select" and a.get("name"):
            self._select = Control(a["name"], "select")
            self.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._select.options.append(a.get("value") or "")
            if "selected" in a:
                self._select.value = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "form":
            self.in_form = False


TRANSLATION_TOKEN = re.compile(r"\[(str\w+)\]")

# Injected statement shapes accepted (see module docstring).
_SET_VALUE = re.compile(r"""^document\.getElementById\('(\w+)'\)\.value\s*=\s*(?:"([^"]*)"|(-?\d+));$""")
_SET_INDEX = re.compile(r"""^document\.getElementById\('(\w+)'\)\.selectedIndex\s*=\s*(\d+);$""")
_SET_CHECKED = re.compile(r"""^document\.getElementById\('(\w+)'\)\.checked\s*=\s*(true|false);$""")
_MAJ_IDENT = re.compile(r"""^MajIdentifiant\((\d+),\s*'(\d*)'\);$""")
_MAJ_PRESENCE = re.compile(r"""^MajPresence\(.*\);$""")
_ADD_BIO = re.compile(r"""^AjouteLecteurBio\((\d+)\);$""")

VALUE_FIELDS = {"id", "flagnbpass", "flagapb", "lastname", "firstname", "ancienTypeIdentifiant",
                "plage", "authdate1", "authdate1hour", "authdate1min", "authdate2", "authdate2hour",
                "authdate2min", "codeSaisi", "nbpassvalue", "passrestantsvalue", "empreinte"}
INDEX_FIELDS = {"authstatus", "groupeAcces1", "groupeAcces2", "groupeAcces3", "typeIdentifiant"}
CHECK_FIELDS = {"authdates", "alarm", "nocontrol", "cycleback", "modeOffice", "codeBadge", "nbpass"}


class UserForm:
    """The user form as the browser would hold it after the page script ran."""

    def __init__(self, controls: List[Control]):
        self.controls = controls
        self.by_name = {c.name: c for c in controls}
        names = [c.name for c in controls]
        if names != EXPECTED_FIELDS:
            raise FormError("The controller's user form has changed (unexpected fields); "
                            "writes are disabled to avoid corrupting users.")

    def __getitem__(self, name: str) -> Control:
        return self.by_name[name]

    def get(self, name: str) -> str:
        c = self.by_name[name]
        if c.kind == "checkbox":
            return "on" if c.checked else "off"
        return c.select_value() if c.kind == "select" else c.value

    def set(self, name: str, value) -> None:
        c = self.by_name[name]
        if c.kind == "checkbox":
            c.checked = bool(value)
        else:
            c.value = str(value)

    def body(self) -> bytes:
        """Exactly what EnvoiPostAjax() sends (UTF-8, no percent-encoding)."""
        return "".join(c.serialized() for c in self.controls).encode("utf-8")

    def values(self) -> Dict[str, str]:
        return {c.name: self.get(c.name) for c in self.controls}


def build_form(html: str, blank_lines: set, group_ids: List[int], slot_ids: List[int],
               translations: Dict[str, str]) -> UserForm:
    """Parse the form page and apply the controller's injected statements."""
    p = _FormParser()
    p.feed(html)
    controls = p.controls

    # Dynamic options added by the page script (Chargement()).
    by = {c.name: c for c in controls}
    for g in ("groupeAcces1", "groupeAcces2", "groupeAcces3"):
        if g in by:
            by[g].options += [str(k) for k in group_ids]
    if "plage" in by:
        by["plage"].options += [str(k) for k in slot_ids]
    for c in controls:  # traduire() replaces [strX] tokens before the form exists
        c.value = TRANSLATION_TOKEN.sub(lambda m: translations.get(m.group(1), m.group(0)), c.value)

    form = UserForm(controls)

    for raw in html.splitlines():
        line = raw.strip()
        if not line or line in blank_lines:
            continue
        m = _SET_VALUE.match(line)
        if m and m.group(1) in VALUE_FIELDS:
            form[m.group(1)].value = m.group(2) if m.group(2) is not None else m.group(3)
            continue
        m = _SET_INDEX.match(line)
        if m and m.group(1) in INDEX_FIELDS:
            c, i = form[m.group(1)], int(m.group(2))
            if i >= len(c.options):
                raise FormError(f"{m.group(1)} points to option {i}, which does not exist.")
            c.value = c.options[i]
            continue
        m = _SET_CHECKED.match(line)
        if m and m.group(1) in CHECK_FIELDS:
            form[m.group(1)].checked = m.group(2) == "true"
            continue
        m = _MAJ_IDENT.match(line)
        if m:
            form["typeIdentifiant"].value = m.group(1)
            form["ident"].value = m.group(2)
            continue
        if _MAJ_PRESENCE.match(line):
            continue  # display only
        m = _ADD_BIO.match(line)
        if m:
            form["listeModuleEnrolement"].options.append(m.group(1))
            continue
        raise FormError(f"Unrecognised content in the controller's user page: {line[:80]!r}")
    return form


# --------------------------------------------------------------------------- requested changes

@dataclass
class UserChange:
    """Fields a caller may set. None means 'leave as it is'."""
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    identifier: Optional[str] = None      # badge/code number, create only
    id_type: Optional[int] = None         # 1..4, create only
    authorised: Optional[bool] = None
    groups: Optional[Tuple[int, int, int]] = None
    time_slot: Optional[int] = None       # 255 = from group, 0 = always, n = slot n
    valid_from: Optional[str] = None      # "dd/mm/yyyy hh:mm"; "" clears both dates
    valid_until: Optional[str] = None


def _check_name(label: str, value: str) -> str:
    value = value.strip()
    if not NAME_RE.match(value):
        raise ValidationError(f"{label} can only contain letters, digits, spaces and . ' ( ) _ -")
    if len(value.encode("utf-8")) > NAME_MAX_BYTES:
        raise ValidationError(f"{label} is too long for the controller (16 bytes; accented letters count as 2).")
    return value


def _split_datetime(label: str, value: str) -> Tuple[str, str, str]:
    value = value.strip()
    try:
        d = datetime.strptime(value, "%d/%m/%Y %H:%M")
    except ValueError:
        raise ValidationError(f"{label} must look like 31/12/2026 18:00.") from None
    return d.strftime("%d/%m/%Y"), str(d.hour), f"{d.minute:02d}"


def apply_change(form: UserForm, ch: UserChange, creating: bool, users: List[User],
                 slot: Optional[int], group_ids: List[int], slot_ids: List[int]) -> None:
    if ch.last_name is not None:
        form.set("lastname", _check_name("Last name", ch.last_name))
    if ch.first_name is not None:
        form.set("firstname", _check_name("First name", ch.first_name))
    if not form.get("lastname") and not form.get("firstname"):
        raise ValidationError("Give at least a last name or a first name.")

    if creating:
        ident = (ch.identifier or "").strip()
        if not re.fullmatch(r"[0-9]{1,17}", ident) or int(ident) == 0:
            raise ValidationError("Badge number must be 1 to 17 digits and not 0.")
        if int(ident) >= 2 ** 56:
            raise ValidationError("Badge number is too large for the controller.")
        clash = next((u for u in users if u.id_type != NO_IDENTIFIER_TYPE and u.identifier == int(ident)), None)
        if clash:
            raise ValidationError(f"Badge {ident} already belongs to user slot {clash.index}.")
        t = ch.id_type or 1
        if t not in ID_TYPES:
            raise ValidationError("Unknown badge type.")
        form.set("ident", ident)
        form.set("typeIdentifiant", t)
    elif ch.identifier is not None or ch.id_type is not None:
        # The controller's own UI makes these read-only once a user exists.
        raise ValidationError("A user's badge number and type cannot be changed; delete and recreate the user.")

    if ch.authorised is not None:
        form.set("authstatus", 0 if ch.authorised else 1)

    if ch.groups is not None:
        if len(ch.groups) != 3:
            raise ValidationError("Give exactly three group slots (0 = none).")
        for i, g in enumerate(ch.groups, start=1):
            if g != 0 and g not in group_ids:
                raise ValidationError(f"Group {g} does not exist.")
            form.set(f"groupeAcces{i}", g)

    if ch.time_slot is not None:
        if ch.time_slot not in (SLOT_TIME_FROM_GROUP, SLOT_ALWAYS) and ch.time_slot not in slot_ids:
            raise ValidationError(f"Time slot {ch.time_slot} does not exist.")
        form.set("plage", ch.time_slot)

    if ch.valid_from is not None or ch.valid_until is not None:
        if (ch.valid_from or "") == "" and (ch.valid_until or "") == "":
            form.set("authdates", False)
            for f in ("authdate1", "authdate1hour", "authdate1min", "authdate2", "authdate2hour", "authdate2min"):
                form.set(f, "")
        else:
            if not ch.valid_from or not ch.valid_until:
                raise ValidationError("Give both a start and an end for the validity period.")
            d1, h1, m1 = _split_datetime("Valid from", ch.valid_from)
            d2, h2, m2 = _split_datetime("Valid until", ch.valid_until)
            if datetime.strptime(ch.valid_until, "%d/%m/%Y %H:%M") <= datetime.strptime(ch.valid_from, "%d/%m/%Y %H:%M"):
                raise ValidationError("The validity period must end after it starts.")
            form.set("authdates", True)
            for f, v in (("authdate1", d1), ("authdate1hour", h1), ("authdate1min", m1),
                         ("authdate2", d2), ("authdate2hour", h2), ("authdate2min", m2)):
                form.set(f, v)


def record_matches_form(u: User, form: UserForm) -> List[str]:
    """Differences between a user's binary record and its form. Empty = consistent."""
    diffs = []
    if form.get("lastname") != u.last_name:
        diffs.append("last name")
    if form.get("firstname") != u.first_name:
        diffs.append("first name")
    if u.id_type != NO_IDENTIFIER_TYPE:
        if form.get("ident") != str(u.identifier):
            diffs.append("badge number")
        if form.get("typeIdentifiant") != str(u.id_type):
            diffs.append("badge type")
    if form.get("authstatus") != str(u.status):
        diffs.append("status")
    for i, g in enumerate(u.groups, start=1):
        if form.get(f"groupeAcces{i}") != str(g):
            diffs.append(f"group {i}")
    return diffs


def form_summary(form: UserForm) -> dict:
    """User-facing view of a form (what the dashboard edits)."""
    v = form.values()
    def dt(d, h, m):
        return f"{v[d]} {int(v[h] or 0):02d}:{int(v[m] or 0):02d}" if v[d] else ""
    return {
        "slot": int(v["id"] or 0),
        "last_name": v["lastname"],
        "first_name": v["firstname"],
        "identifier": v["ident"],
        "id_type": int(v["typeIdentifiant"] or 1),
        "authorised": v["authstatus"] == "0",
        "groups": [int(v["groupeAcces1"] or 0), int(v["groupeAcces2"] or 0), int(v["groupeAcces3"] or 0)],
        "time_slot": int(v["plage"] or SLOT_TIME_FROM_GROUP),
        "valid": v["authdates"] == "on",
        "valid_from": dt("authdate1", "authdate1hour", "authdate1min") if v["authdates"] == "on" else "",
        "valid_until": dt("authdate2", "authdate2hour", "authdate2min") if v["authdates"] == "on" else "",
        "has_other_settings": any(v[k] == "on" for k in ("alarm", "nocontrol", "cycleback", "modeOffice", "codeBadge", "nbpass")),
    }


# --------------------------------------------------------------------------- responses

_CALL_AT_END = re.compile(r"(\w+)\(([^()]*)\)\s*;?\s*</script>\s*$", re.S)
_ERROR_CASE = re.compile(r"case\s+(-?\d+)\s*:[^\n]*\n\s*message\s*=\s*['\"]\[(str\w+)\]['\"]")
_ERROR_DEFAULT = re.compile(r"var\s+message\s*=\s*['\"]\[(str\w+)\]['\"]")


def interpret_response(text: str, translations: Dict[str, str]) -> Tuple[bool, str]:
    """Read the script the controller returns: it ends with Erreur(n) on failure.

    The error texts come from the response's own switch statement, so new
    error codes are reported without code changes.
    """
    text = text.lstrip("﻿")
    m = _CALL_AT_END.search(text)
    if not m:
        return True, ""  # no trailing call: nothing says it failed; the Users diff decides
    func, arg = m.group(1), m.group(2).strip().strip("'\"")
    if func != "Erreur":
        return True, func
    messages = {code: key for code, key in _ERROR_CASE.findall(text)}
    key = messages.get(arg)
    if key is None:
        d = _ERROR_DEFAULT.search(text)
        key = d.group(1) if d else ""
    reason = translations.get(key, key) if key else "unknown error"
    return False, f"{reason.strip()} (code {arg})"


# --------------------------------------------------------------------------- manager

def diff_users_file(before: bytes, after: bytes) -> List[int]:
    """Slots (1-based) whose 49-byte record differs."""
    n = max(len(before), len(after)) // USER_RECORD_SIZE
    changed = []
    for s in range(n):
        a = before[s * USER_RECORD_SIZE:(s + 1) * USER_RECORD_SIZE]
        b = after[s * USER_RECORD_SIZE:(s + 1) * USER_RECORD_SIZE]
        if a.ljust(USER_RECORD_SIZE, b"\0") != b.ljust(USER_RECORD_SIZE, b"\0"):
            changed.append(s + 1)
    return changed


class UserManager:
    """One write at a time, each verified against the Users file afterwards."""

    def __init__(self, client, translations, labels_groups, labels_slots, audit=None):
        self.client = client
        self.translations = translations      # callables returning fresh data
        self.groups = labels_groups
        self.slots = labels_slots
        self.audit = audit or (lambda entry: None)
        self.lock = WRITE_LOCK
        self._blank_lines: Optional[set] = None

    # ---- reading ---------------------------------------------------------
    def _ids(self) -> Tuple[List[int], List[int]]:
        return sorted(self.groups()), sorted(self.slots())

    def _blank(self) -> set:
        if self._blank_lines is None:
            html = self.client.get(FORM_PATH, "id=-1").decode("utf-8", errors="replace")
            self._blank_lines = {l.strip() for l in html.splitlines()}
        return self._blank_lines

    def _form_for(self, u: Optional[User]) -> UserForm:
        if u is None:
            query = "id=-1"
        else:
            ident = "-" if u.id_type == NO_IDENTIFIER_TYPE else str(u.identifier)
            query = f"id={u.index:05d}&type={u.id_type}&ident={ident}"
        html = self.client.get(FORM_PATH, query).decode("utf-8", errors="replace")
        gids, sids = self._ids()
        return build_form(html, self._blank(), gids, sids, self.translations())

    def _users_raw(self) -> bytes:
        return self.client.get("/assets/Users")

    def read(self, slot: int) -> dict:
        users = {u.index: u for u in parse_users(self._users_raw())}
        u = users.get(slot)
        if u is None:
            raise ValidationError(f"No user in slot {slot}.")
        form = self._form_for(u)
        diffs = record_matches_form(u, form)
        out = form_summary(form)
        out["consistent"] = not diffs
        out["inconsistencies"] = diffs
        return out

    # ---- writing ---------------------------------------------------------
    def create(self, ch: UserChange, dry_run: bool = False) -> dict:
        with self.lock:
            before = self._users_raw()
            users = parse_users(before)
            form = self._form_for(None)
            if form.get("id") != "0":
                raise FormError("The new-user form did not come back empty.")
            gids, sids = self._ids()
            apply_change(form, ch, True, users, None, gids, sids)
            body = form.body()
            if dry_run:
                return {"dry_run": True, "body": body.decode("utf-8")}
            response = self.client.post_form(UPDATE_PATH, body).decode("utf-8", errors="replace")
            ok, detail = interpret_response(response, self.translations())
            after = self._users_raw()
            changed = diff_users_file(before, after)
            new = [u for u in parse_users(after) if u.index in changed]
            # Names, badge, type, status and groups must match what we sent.
            mismatches = record_matches_form(new[0], form) if len(new) == 1 else ["record"]
            self.audit({"action": "create", "changed_slots": changed, "sent": form_summary(form),
                        "controller_ok": ok, "controller_detail": detail, "response": response})
            if not ok and not changed:
                raise WriteFailed(f"The controller refused: {detail}.")
            if len(changed) != 1 or len(new) != 1 or mismatches:
                raise WriteFailed(f"Created user could not be verified (changed slots {changed}, "
                                  f"mismatches {mismatches}). Check the controller.")
            return {"slot": new[0].index}

    def update(self, slot: int, ch: UserChange, dry_run: bool = False) -> dict:
        with self.lock:
            before = self._users_raw()
            users = parse_users(before)
            u = next((x for x in users if x.index == slot), None)
            if u is None:
                raise ValidationError(f"No user in slot {slot}.")
            form = self._form_for(u)
            diffs = record_matches_form(u, form)
            if diffs:
                raise FormError(f"The controller's page for slot {slot} disagrees with its user list "
                                f"({', '.join(diffs)}); not editing.")
            if form.get("id") != str(slot):
                raise FormError(f"The controller returned the form of slot {form.get('id')}, not {slot}.")
            previous = form.values()
            gids, sids = self._ids()
            apply_change(form, ch, False, users, slot, gids, sids)
            body = form.body()
            if dry_run:
                return {"dry_run": True, "body": body.decode("utf-8")}
            response = self.client.post_form(UPDATE_PATH, body).decode("utf-8", errors="replace")
            ok, detail = interpret_response(response, self.translations())
            after = self._users_raw()
            changed = diff_users_file(before, after)
            now = next((x for x in parse_users(after) if x.index == slot), None)
            mismatches = record_matches_form(now, form) if now else ["record missing"]
            self.audit({"action": "update", "slot": slot, "changed_slots": changed,
                        "previous_form": previous, "sent": form_summary(form),
                        "controller_ok": ok, "controller_detail": detail, "response": response})
            if not ok and not changed:
                raise WriteFailed(f"The controller refused: {detail}.")
            if any(s != slot for s in changed) or mismatches:
                raise WriteFailed(f"Update could not be verified (changed slots {changed}, "
                                  f"mismatches {mismatches}). Check the controller.")
            return {"slot": slot, "changed": bool(changed)}

    def delete(self, slot: int, dry_run: bool = False) -> dict:
        with self.lock:
            before = self._users_raw()
            u = next((x for x in parse_users(before) if x.index == slot), None)
            if u is None:
                raise ValidationError(f"No user in slot {slot}.")
            form = self._form_for(u)
            if form.get("id") != str(slot) or record_matches_form(u, form):
                raise FormError(f"The controller's page for slot {slot} does not match; not deleting.")
            query = f"type=11&index={form.get('id')}"
            if dry_run:
                return {"dry_run": True, "query": query}
            previous = form.values()
            response = self.client.get_action(DELETE_PATH, query).decode("utf-8", errors="replace")
            ok, detail = interpret_response(response, self.translations())
            after = self._users_raw()
            changed = diff_users_file(before, after)
            still = next((x for x in parse_users(after) if x.index == slot), None)
            self.audit({"action": "delete", "slot": slot, "changed_slots": changed,
                        "previous_form": previous,
                        "controller_ok": ok, "controller_detail": detail, "response": response})
            if not ok and not changed:
                raise WriteFailed(f"The controller refused: {detail}.")
            if changed != [slot] or still is not None:
                raise WriteFailed(f"Delete could not be verified (changed slots {changed}). Check the controller.")
            return {"slot": slot, "deleted": True}
