"""Access groups and time slots ("plages") on the Vauban controller.

The controller has fixed tables: 1,024 groups and 128 time slots. Nothing is
created or deleted; an "unused" entry is renamed and configured instead.

* A time slot is a weekly schedule: up to 10 ranges "HH:MM-HH:MM" for each day,
  plus a row used on public holidays (/FR/horaires-plage.cgi, form type 4).
* A group says, for each door, "no access" (255), "any time" (0) or which time
  slot applies, and can be switched off ("forbidden")
  (/FR/horaires-groupe.cgi, form type 2).

Same safety model as users.py: the page's own form gives the fields; only a
closed set of injected statements is accepted; the body is sent exactly like
the browser (EnvoiPostAjax); after saving, the page is read again and must
show what was sent, and no other entry's name may have changed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .users import WRITE_LOCK, FormError, ValidationError, WriteFailed, interpret_response

GROUP_PATH = "/FR/horaires-groupe.cgi"
SLOT_PATH = "/FR/horaires-plage.cgi"
UPDATE_PATH = "/FR/update.cgi"

GROUP_COUNT = 1024
SLOT_COUNT = 128
NO_ACCESS = 255
ANY_TIME = 0
NAME_MAX = 20

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche", "jourferie"]
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday"]
RANGES_PER_DAY = 10

GROUP_FIXED_FIELDS = ["type", "id", "libelle", "isforbidden", "lecteurtempo", "lecteurtempo", "temporisation",
                      "limitaccess", "limitaccess", "maxaccess", "modifyaccesscount", "accesscount"]
SLOT_FIELDS = ["type", "id", "libelle", "typeAffichage"] + [f"{d}{i}" for d in DAYS for i in range(1, RANGES_PER_DAY + 1)]

NAME_RE = re.compile(r"^[0-9A-Za-zÀ-ÖØ-öø-ÿŒœ .'()/_+-]*$")
RANGE_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- generic form

@dataclass
class Control:
    name: str
    kind: str                       # hidden, text, number, checkbox, radio, select
    value: str = ""
    checked: bool = False
    options: List[str] = field(default_factory=list)
    id: str = ""

    def serialized(self) -> str:
        if self.kind == "checkbox":
            return f"{self.name}={'on' if self.checked else 'off'}&"
        if self.kind == "radio":
            return f"{self.name}={self.value}&" if self.checked else ""
        if self.kind == "select":
            v = self.value if self.value in self.options else (self.options[0] if self.options else "")
            return f"{self.name}={v}&"
        return f"{self.name}={self.value}&"


class _Parser(HTMLParser):
    def __init__(self, form_id: str):
        super().__init__(convert_charrefs=True)
        self.form_id, self.inside, self.controls, self._sel = form_id, False, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == self.form_id:
            self.inside = True
        if not self.inside:
            return
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("button", "submit", "reset", "image") or not a.get("name"):
                return
            self.controls.append(Control(a["name"], kind, a.get("value") or "", "checked" in a, id=a.get("id", "")))
        elif tag == "select" and a.get("name"):
            self._sel = Control(a["name"], "select", id=a.get("id", ""))
            self.controls.append(self._sel)
        elif tag == "option" and self._sel is not None:
            self._sel.options.append(a.get("value") or "")
            if "selected" in a:
                self._sel.value = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._sel = None
        elif tag == "form":
            self.inside = False


class Form:
    def __init__(self, controls: List[Control]):
        self.controls = controls

    def by_id(self, ident: str) -> Control:
        for c in self.controls:
            if (c.id or c.name) == ident:
                return c
        raise FormError(f"The page has no field {ident!r}.")

    def get(self, name: str) -> str:
        for c in self.controls:
            if c.name == name:
                if c.kind == "checkbox":
                    return "on" if c.checked else "off"
                if c.kind == "radio":
                    continue
                if c.kind == "select":
                    return c.value if c.value in c.options else (c.options[0] if c.options else "")
                return c.value
        radios = [c for c in self.controls if c.name == name and c.kind == "radio" and c.checked]
        if radios:
            return radios[0].value
        raise FormError(f"The page has no field {name!r}.")

    def set(self, name: str, value) -> None:
        for c in self.controls:
            if c.name == name and c.kind != "radio":
                if c.kind == "checkbox":
                    c.checked = bool(value)
                elif c.kind == "select" and str(value) not in c.options:
                    raise ValidationError(f"{value!r} is not a valid choice for {name}.")
                else:
                    c.value = str(value)
                return
        raise FormError(f"The page has no field {name!r}.")

    def body(self) -> bytes:
        return "".join(c.serialized() for c in self.controls).encode("utf-8")

    def values(self) -> Dict[str, str]:
        out = {}
        for c in self.controls:
            if c.kind == "radio":
                if c.checked:
                    out[c.name] = c.value
            else:
                out[c.name] = self.get(c.name)
        return out


_SET = re.compile(r"""^document\.getElementById\('(\w+)'\)\.(value|checked)\s*=\s*(?:"([^"]*)"|(-?\d+)|(true|false));$""")
_ADD_READER = re.compile(r"^AjouteLecteur\((\d+)(?:,\s*-?\d+)?\);$")
_SHOW_LIFTS = re.compile(r"^AfficheAscenseurs\(\);$")


def _injected_lines(html: str, blank_lines: set) -> List[str]:
    return [l.strip() for l in html.splitlines() if l.strip() and l.strip() not in blank_lines]


# --------------------------------------------------------------------------- groups

def build_group_form(html: str, blank_lines: set, slot_ids: Sequence[int], lift_ids: Sequence[int]) -> Tuple[Form, List[int]]:
    """Parse a group page. Returns the form and the door numbers it lists."""
    p = _Parser("formGroupes")
    p.feed(html)
    controls = p.controls
    if [c.name for c in controls] != GROUP_FIXED_FIELDS:
        raise FormError("The controller's group form has changed; editing groups is disabled.")
    # AjouteLecteur(i) adds two selects per door (also present on the blank page).
    doors = [int(m.group(1)) for l in html.splitlines() if (m := _ADD_READER.match(l.strip()))]
    if not doors or len(set(doors)) != len(doors):
        raise FormError("The group page doesn't list the doors as expected.")
    for d in doors:
        controls.append(Control(f"autorisationLecteur{d}", "select",
                                options=[str(NO_ACCESS), str(ANY_TIME)] + [str(k) for k in slot_ids],
                                id=f"autorisationLecteur{d}"))
        controls.append(Control(f"ascenseurLecteur{d}", "select", options=["0"] + [str(k) for k in lift_ids],
                                id=f"ascenseurLecteur{d}"))
    form = Form(controls)
    allowed_values = {"id", "temporisation", "maxaccess", "accesscount"} | \
                     {f"autorisationLecteur{d}" for d in doors} | {f"ascenseurLecteur{d}" for d in doors}
    allowed_checks = {"isforbidden", "modifyaccesscount", "lecteurtempo1", "lecteurtempo2", "limitaccess1", "limitaccess2"}
    for line in _injected_lines(html, blank_lines):
        if _ADD_READER.match(line) or _SHOW_LIFTS.match(line):
            continue
        m = _SET.match(line)
        if not m:
            raise FormError(f"Unrecognised content in the controller's group page: {line[:80]!r}")
        ident, prop, sval, ival, bval = m.groups()
        if prop == "value" and ident in allowed_values and bval is None:
            c = form.by_id(ident)
            v = sval if sval is not None else ival
            if c.kind == "select" and v not in c.options:
                raise FormError(f"{ident} is set to {v}, which is not one of its choices.")
            c.value = v
        elif prop == "checked" and ident in allowed_checks and bval is not None:
            c = form.by_id(ident)
            if c.kind == "radio":
                for other in form.controls:
                    if other.kind == "radio" and other.name == c.name:
                        other.checked = False
            c.checked = bval == "true"
        else:
            raise FormError(f"Unrecognised content in the controller's group page: {line[:80]!r}")
    return form, doors


def group_summary(form: Form, doors: Sequence[int], name: str) -> dict:
    v = form.values()
    return {
        "id": int(v["id"]),
        "name": name,
        "enabled": v["isforbidden"] == "off",
        "doors": {str(d): int(v[f"autorisationLecteur{d}"]) for d in doors},
        "has_other_settings": (v.get("lecteurtempo") != "1" or v.get("limitaccess") != "0"
                               or v["modifyaccesscount"] == "on"
                               or any(v[f"ascenseurLecteur{d}"] != "0" for d in doors)),
    }


# --------------------------------------------------------------------------- time slots

def build_slot_form(html: str, blank_lines: set) -> Form:
    p = _Parser("formPlage")
    p.feed(html)
    if [c.name for c in p.controls] != SLOT_FIELDS:
        raise FormError("The controller's time-slot form has changed; editing time slots is disabled.")
    form = Form(p.controls)
    allowed = {"id"} | set(SLOT_FIELDS[4:])
    for line in _injected_lines(html, blank_lines):
        m = _SET.match(line)
        if not m or m.group(2) != "value" or m.group(1) not in allowed or m.group(5) is not None:
            raise FormError(f"Unrecognised content in the controller's time-slot page: {line[:80]!r}")
        v = m.group(3) if m.group(3) is not None else m.group(4)
        if m.group(1) != "id" and v and not RANGE_RE.match(v):
            raise FormError(f"The controller returned a time range we can't read: {v!r}")
        form.by_id(m.group(1)).value = v
    return form


def slot_days(form: Form) -> Dict[str, List[str]]:
    v = form.values()
    return {key: [v[f"{day}{i}"] for i in range(1, RANGES_PER_DAY + 1) if v[f"{day}{i}"]]
            for day, key in zip(DAYS, DAY_KEYS)}


def normalise_ranges(label: str, ranges: Sequence[str]) -> List[str]:
    """Validate one day's ranges: HH:MM-HH:MM, start before end, no overlaps, max 10."""
    out = []
    for r in ranges:
        r = r.strip().replace(" ", "").replace("–", "-")
        if not r:
            continue
        m = RANGE_RE.match(r)
        if not m:
            raise ValidationError(f"{label}: “{r}” must look like 08:00-12:30.")
        start, end = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
        if end <= start:
            raise ValidationError(f"{label}: “{r}” must end after it starts. For a night shift, "
                                  f"split it over two days (e.g. 22:00-23:59 and 00:00-06:00).")
        out.append((start, end, r))
    out.sort()
    for (s1, e1, r1), (s2, e2, r2) in zip(out, out[1:]):
        if s2 < e1:
            raise ValidationError(f"{label}: “{r1}” and “{r2}” overlap.")
    if len(out) > RANGES_PER_DAY:
        raise ValidationError(f"{label}: at most {RANGES_PER_DAY} time ranges per day.")
    return [r for _, _, r in out]


def check_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("Give it a name.")
    if not NAME_RE.match(value):
        raise ValidationError("The name can only contain letters, digits, spaces and . ' ( ) / _ + -")
    if len(value.encode("utf-8")) > NAME_MAX:
        raise ValidationError(f"The name is too long for the controller ({NAME_MAX} bytes; accented letters count as 2).")
    return value


# --------------------------------------------------------------------------- manager

class ScheduleManager:
    """Reads and writes groups and time slots, one write at a time, each verified."""

    def __init__(self, client, translations: Callable, group_labels: Callable, slot_labels: Callable,
                 lift_labels: Callable, refresh_labels: Callable, audit: Optional[Callable] = None):
        self.client = client
        self.translations = translations
        self.group_labels = group_labels
        self.slot_labels = slot_labels
        self.lift_labels = lift_labels
        self.refresh_labels = refresh_labels      # drop cached Groupes / Plages labels
        self.audit = audit or (lambda e: None)
        self._blank: Dict[str, set] = {}

    def _page(self, path: str, index: int) -> str:
        return self.client.get(path, f"index={int(index)}").decode("utf-8", errors="replace")

    def _blank_lines(self, path: str) -> set:
        if path not in self._blank:
            self._blank[path] = {l.strip() for l in self._page(path, -1).splitlines()}
        return self._blank[path]

    # ---- groups --------------------------------------------------------
    def group_form(self, gid: int) -> Tuple[Form, List[int]]:
        if not 1 <= gid <= GROUP_COUNT:
            raise ValidationError(f"There is no group {gid}.")
        form, doors = build_group_form(self._page(GROUP_PATH, gid), self._blank_lines(GROUP_PATH),
                                       sorted(self.slot_labels()), sorted(self.lift_labels()))
        if form.get("id") != str(gid):
            raise FormError(f"The controller returned group {form.get('id')} instead of {gid}.")
        return form, doors

    def read_group(self, gid: int) -> dict:
        form, doors = self.group_form(gid)
        return group_summary(form, doors, self.group_labels().get(gid, ""))

    def update_group(self, gid: int, name: Optional[str], enabled: Optional[bool],
                     door_access: Optional[Dict[int, int]]) -> dict:
        with WRITE_LOCK:
            self.refresh_labels()
            form, doors = self.group_form(gid)
            labels_before = dict(self.group_labels())
            previous = form.values()
            new_name = check_name(name) if name is not None else labels_before.get(gid, "")
            form.set("libelle", new_name)
            if enabled is not None:
                form.set("isforbidden", not enabled)
            slots = set(self.slot_labels())
            for door, value in (door_access or {}).items():
                if door not in doors:
                    raise ValidationError(f"There is no door {door} on this controller.")
                if value not in (NO_ACCESS, ANY_TIME) and value not in slots:
                    raise ValidationError(f"Time slot {value} does not exist.")
                form.set(f"autorisationLecteur{door}", value)
            expected = form.values()
            reply = self.client.post_form(UPDATE_PATH, form.body()).decode("utf-8", errors="replace")
            ok, detail = interpret_response(reply, self.translations())
            self.refresh_labels()
            after_form, _ = self.group_form(gid)
            labels_after = self.group_labels()
            mismatch = [k for k, v in expected.items() if k != "libelle" and after_form.values().get(k) != v]
            if labels_after.get(gid) != new_name:
                mismatch.append("name")
            others = [k for k in set(labels_before) | set(labels_after)
                      if k != gid and labels_before.get(k) != labels_after.get(k)]
            self.audit({"action": "update_group", "group": gid, "previous_form": previous,
                        "previous_name": labels_before.get(gid), "sent": expected,
                        "controller_ok": ok, "controller_detail": detail, "response": reply[:2000],
                        "mismatch": mismatch, "other_groups_renamed": others})
            if not ok:
                raise WriteFailed(f"The controller refused: {detail}.")
            if mismatch or others:
                raise WriteFailed(f"The group could not be verified after saving (differences: "
                                  f"{', '.join(mismatch + [f'group {o}' for o in others])}). Check it on the controller.")
            return group_summary(after_form, doors, labels_after.get(gid, ""))

    # ---- time slots ----------------------------------------------------
    def slot_form(self, sid: int) -> Form:
        if not 1 <= sid <= SLOT_COUNT:
            raise ValidationError(f"There is no time slot {sid}.")
        form = build_slot_form(self._page(SLOT_PATH, sid), self._blank_lines(SLOT_PATH))
        if form.get("id") != str(sid):
            raise FormError(f"The controller returned time slot {form.get('id')} instead of {sid}.")
        return form

    def read_slot(self, sid: int) -> dict:
        return {"id": sid, "name": self.slot_labels().get(sid, ""), "days": slot_days(self.slot_form(sid))}

    def update_slot(self, sid: int, name: Optional[str], days: Optional[Dict[str, List[str]]]) -> dict:
        with WRITE_LOCK:
            self.refresh_labels()
            form = self.slot_form(sid)
            labels_before = dict(self.slot_labels())
            previous = form.values()
            new_name = check_name(name) if name is not None else labels_before.get(sid, "")
            form.set("libelle", new_name)
            form.set("typeAffichage", "0")       # every day sent explicitly
            labels = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
                      "fri": "Friday", "sat": "Saturday", "sun": "Sunday", "holiday": "Public holidays"}
            for day, key in zip(DAYS, DAY_KEYS):
                if days is None or key not in days:
                    continue
                ranges = normalise_ranges(labels[key], days[key])
                for i in range(1, RANGES_PER_DAY + 1):
                    form.set(f"{day}{i}", ranges[i - 1] if i <= len(ranges) else "")
            expected = form.values()
            reply = self.client.post_form(UPDATE_PATH, form.body()).decode("utf-8", errors="replace")
            ok, detail = interpret_response(reply, self.translations())
            self.refresh_labels()
            after = self.slot_form(sid)
            labels_after = self.slot_labels()
            got = after.values()
            mismatch = [k for k in SLOT_FIELDS[4:] if got.get(k) != expected.get(k)]
            if labels_after.get(sid) != new_name:
                mismatch.append("name")
            others = [k for k in set(labels_before) | set(labels_after)
                      if k != sid and labels_before.get(k) != labels_after.get(k)]
            self.audit({"action": "update_time_slot", "time_slot": sid, "previous_form": previous,
                        "previous_name": labels_before.get(sid), "sent": expected,
                        "controller_ok": ok, "controller_detail": detail, "response": reply[:2000],
                        "mismatch": mismatch, "other_slots_renamed": others})
            if not ok:
                raise WriteFailed(f"The controller refused: {detail}.")
            if mismatch or others:
                raise WriteFailed(f"The time slot could not be verified after saving (differences: "
                                  f"{', '.join(mismatch + [f'slot {o}' for o in others])}). Check it on the controller.")
            return {"id": sid, "name": labels_after.get(sid, ""), "days": slot_days(after)}
