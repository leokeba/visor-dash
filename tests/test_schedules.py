"""Offline tests for groups and time slots (synthetic controller).  Run: python3 -m unittest -v"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from visor_dash.schedules import (DAY_KEYS, DAYS, GROUP_FIXED_FIELDS, NO_ACCESS, SLOT_FIELDS, FormError,
                                  ScheduleManager, ValidationError, WriteFailed, build_group_form,
                                  build_slot_form, normalise_ranges)
from visor_dash.server import Dashboard, make_handler
from tests.test_dashboard import FakeClient, user_record
from visor_dash.client import parse_users

DOORS = (1, 2)


def group_page(gid, g, extra=""):
    ctl = {
        "type": '<input type="hidden" name="type" id="type" value="2" />',
        "id": '<input type="hidden" name="id" id="id" value="0" />',
        "libelle": '<input type="text" name="libelle" id="libelle" value="[strGROUPE]" maxlength="20" />',
        "isforbidden": '<input type="checkbox" name="isforbidden" id="isforbidden" />',
        "temporisation": '<input type="number" name="temporisation" id="temporisation" value="0" />',
        "maxaccess": '<input type="number" name="maxaccess" id="maxaccess" value="0" />',
        "modifyaccesscount": '<input type="checkbox" name="modifyaccesscount" id="modifyaccesscount" />',
        "accesscount": '<input type="number" name="accesscount" id="accesscount" value="0" />',
    }
    html = ['<form id="formGroupes" name="formGroupes">']
    seen = set()
    for name in GROUP_FIXED_FIELDS:
        if name == "lecteurtempo" and name not in seen:
            html += ['<input type="radio" name="lecteurtempo" id="lecteurtempo1" value="1" checked="checked" />',
                     '<input type="radio" name="lecteurtempo" id="lecteurtempo2" value="0" />']
        elif name == "limitaccess" and name not in seen:
            html += ['<input type="radio" name="limitaccess" id="limitaccess1" value="1" />',
                     '<input type="radio" name="limitaccess" id="limitaccess2" value="0" checked="checked" />']
        elif name in ctl:
            html.append(ctl[name])
        seen.add(name)
    html.append('<table id="listeLecteur"></table></form>\n<script type="text/javascript">\nfunction ChargementPlages()\n{')
    if gid is not None:
        html.append(f"document.getElementById('id').value={gid};")
    html += [f"AjouteLecteur({d},0);" for d in DOORS]
    if gid is not None:
        if g["forbidden"]:
            html.append("document.getElementById('isforbidden').checked=true;")
        for d, v in g["doors"].items():
            if v != NO_ACCESS:
                html.append(f"document.getElementById('autorisationLecteur{d}').value={v};")
    html.append(extra)
    html.append('document.getElementById("libelle").value = dicoGroupes[document.getElementById("id").value];\n}\n</script>')
    return "\n".join(html)


def slot_page(sid, days, extra=""):
    html = ['<form id="formPlage" name="formPlage">',
            '<input type="hidden" name="type" id="type" value="4" />',
            '<input type="hidden" name="id" id="id" value="0" />',
            '<input type="text" name="libelle" id="libelle" value="[strPLAGE]" maxlength="20" />',
            '<select id="typeAffichage" name="typeAffichage"><option value="0">a</option><option value="1">b</option>'
            '<option value="2">c</option><option value="3">d</option></select>']
    html += [f'<input type="text" class="edit-in-modal" id="{n}" name="{n}" readonly="readonly"/>' for n in SLOT_FIELDS[4:]]
    html.append('</form>\n<script type="text/javascript">')
    if sid is not None:
        html.append(f"\tdocument.getElementById('id').value={sid};")
        for day, key in zip(DAYS, DAY_KEYS):
            for i, r in enumerate(days.get(key, []), start=1):
                html.append(f'document.getElementById(\'{day}{i}\').value="{r}";')
    html.append(extra)
    html.append('\tdocument.getElementById("libelle").value = dicoPlages[document.getElementById("id").value];\n</script>')
    return "\n".join(html)


class FakeScheduleController:
    """Serves group/time-slot pages from a store and applies posted forms to it."""

    def __init__(self, ignore_post=False):
        self.groups = {g: {"name": "", "forbidden": True, "doors": {d: NO_ACCESS for d in DOORS}} for g in range(1, 1025)}
        self.slots = {s: {"name": "", "days": {}} for s in range(1, 129)}
        self.groups[1].update(name="Staff", forbidden=False, doors={1: 0, 2: 1})
        self.slots[1].update(name="Office hours", days={"mon": ["08:00-18:00"]})
        self.ignore_post, self.posts = ignore_post, []

    def get(self, path, query=""):
        idx = int(query.split("=")[1])
        if path.endswith("horaires-groupe.cgi"):
            return group_page(None if idx < 0 else idx, self.groups.get(idx)).encode()
        if path.endswith("horaires-plage.cgi"):
            return slot_page(None if idx < 0 else idx, self.slots.get(idx, {}).get("days", {})).encode()
        raise AssertionError(path)

    def post_form(self, path, body):
        self.posts.append(body)
        if self.ignore_post:
            return b"<script>\nSucces();\n</script>"
        f = dict(kv.split("=", 1) for kv in body.decode().rstrip("&").split("&"))
        i = int(f["id"])
        if f["type"] == "2":
            self.groups[i] = {"name": f["libelle"], "forbidden": f["isforbidden"] == "on",
                              "doors": {d: int(f[f"autorisationLecteur{d}"]) for d in DOORS}}
        else:
            self.slots[i] = {"name": f["libelle"], "days": {key: [f[f"{day}{n}"] for n in range(1, 11) if f[f"{day}{n}"]]
                                                           for day, key in zip(DAYS, DAY_KEYS)}}
        return b"<script>\nSucces();\n</script>"

    def labels(self, kind):
        store = self.groups if kind == "g" else self.slots
        return {k: (v["name"] or (f"Groupe {k}" if kind == "g" else f"Plage {k}")) for k, v in store.items()}


def manager(ctl, audit=None):
    return ScheduleManager(ctl, lambda: {}, lambda: ctl.labels("g"), lambda: ctl.labels("s"),
                           lambda: {1: "Lift 1"}, lambda: None, audit)


BLANK_G = {l.strip() for l in group_page(None, None).splitlines()}
BLANK_S = {l.strip() for l in slot_page(None, {}).splitlines()}


class ParseTests(unittest.TestCase):
    def test_group_form_and_body(self):
        g = {"forbidden": False, "doors": {1: 0, 2: 3}}
        form, doors = build_group_form(group_page(7, g), BLANK_G, [1, 2, 3], [1])
        self.assertEqual(doors, [1, 2])
        v = form.values()
        self.assertEqual((v["id"], v["isforbidden"], v["autorisationLecteur1"], v["autorisationLecteur2"]), ("7", "off", "0", "3"))
        body = form.body().decode()
        keys = [kv.split("=")[0] for kv in body.rstrip("&").split("&")]
        self.assertEqual(keys, ["type", "id", "libelle", "isforbidden", "lecteurtempo", "temporisation", "limitaccess",
                                "maxaccess", "modifyaccesscount", "accesscount", "autorisationLecteur1",
                                "ascenseurLecteur1", "autorisationLecteur2", "ascenseurLecteur2"])
        self.assertIn("&lecteurtempo=1&", body)          # only the checked radio is sent
        self.assertIn("&limitaccess=0&", body)

    def test_unknown_group_statement_refused(self):
        g = {"forbidden": True, "doors": {1: 255, 2: 255}}
        for extra in ("document.getElementById('newThing').value=1;", "SomethingElse(2);",
                      "document.getElementById('autorisationLecteur1').value=99;"):   # not a valid choice
            with self.assertRaises(FormError, msg=extra):
                build_group_form(group_page(3, g, extra), BLANK_G, [1, 2], [1])

    def test_slot_form(self):
        days = {"mon": ["07:00-12:00", "13:00-19:00"], "holiday": ["09:00-12:00"]}
        form = build_slot_form(slot_page(4, days), BLANK_S)
        v = form.values()
        self.assertEqual((v["lundi1"], v["lundi2"], v["lundi3"], v["jourferie1"]), ("07:00-12:00", "13:00-19:00", "", "09:00-12:00"))
        with self.assertRaises(FormError):
            build_slot_form(slot_page(4, {"mon": ["7h-12h"]}), BLANK_S)

    def test_ranges(self):
        self.assertEqual(normalise_ranges("Mon", ["14:00-18:00", " 08:00 - 12:00 ", ""]), ["08:00-12:00", "14:00-18:00"])
        for bad in (["08:00-12:00", "11:00-13:00"], ["22:00-06:00"], ["8:00-12:00"], ["24:00-24:30"],
                    ["10:00-10:00"], [f"{h:02d}:00-{h:02d}:30" for h in range(11)]):
            with self.assertRaises(ValidationError, msg=bad):
                normalise_ranges("Mon", bad)


class ManagerTests(unittest.TestCase):
    def test_update_group_and_slot(self):
        ctl = FakeScheduleController()
        log = []
        m = manager(ctl, log.append)
        g = m.update_group(5, "Cleaners", True, {1: 1, 2: NO_ACCESS})
        self.assertEqual((g["name"], g["enabled"], g["doors"]), ("Cleaners", True, {"1": 1, "2": NO_ACCESS}))
        s = m.update_slot(2, "Evenings", {"mon": ["18:00-21:00"], "fri": ["17:00-22:00", "06:00-08:00"]})
        self.assertEqual(s["days"]["fri"], ["06:00-08:00", "17:00-22:00"])
        self.assertEqual(ctl.groups[1]["name"], "Staff")             # others untouched
        self.assertEqual([e["action"] for e in log], ["update_group", "update_time_slot"])
        self.assertEqual(log[0]["previous_form"]["isforbidden"], "on")

    def test_unknown_door_or_slot_refused(self):
        m = manager(FakeScheduleController())
        with self.assertRaises(ValidationError):
            m.update_group(5, "X", True, {3: 0})
        with self.assertRaises(ValidationError):
            m.update_group(5, "X", True, {1: 200})
        with self.assertRaises(ValidationError):
            m.update_group(5, "Way too long a name for it", True, None)

    def test_ignored_write_is_detected(self):
        m = manager(FakeScheduleController(ignore_post=True), lambda e: None)
        with self.assertRaises(WriteFailed):
            m.update_group(5, "Cleaners", True, {1: 0})
        with self.assertRaises(WriteFailed):
            m.update_slot(3, "Nights", {"mon": ["20:00-23:00"]})


class ScheduleClient(FakeClient):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl

    def get(self, path, query=""):
        if "horaires-jours-feries" in path:
            return b"<script>\n</script>"
        return self.ctl.get(path, query)

    def post_form(self, path, body):
        return self.ctl.post_form(path, body)

    def labels(self, name, prefix):
        if name == "Groupes":
            return self.ctl.labels("g")
        if name == "Plages":
            return self.ctl.labels("s")
        if name == "Lifts":
            return {1: "Lift 1"}
        return {1: "Front door", 2: "Office"}

    def users(self):
        return parse_users(user_record(555, 1, (1, 2, 0), "A", "B", 0))   # person in groups 1 and 2


class DashboardScheduleTests(unittest.TestCase):
    def test_access_overview_and_free_entries(self):
        ctl = FakeScheduleController()
        d = Dashboard(ScheduleClient(ctl), "en")
        a = d.access()
        self.assertEqual([g["id"] for g in a["groups"]], [1, 2])     # named or with people
        self.assertEqual(a["groups"][0]["members"], 1)
        self.assertEqual([s["id"] for s in a["time_slots"]], [1])
        self.assertEqual(a["time_slots"][0]["used_by"], [1])
        self.assertEqual(d.free_group(), 3)                          # 1 named, 2 has a member
        self.assertEqual(d.free_slot(), 2)


class HttpScheduleTests(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeScheduleController()
        self.dash = Dashboard(ScheduleClient(self.ctl), "en")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dash, None, None))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Dashboard": "1"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_editing_switch_and_create(self):
        self.assertEqual(self.post("/api/groups/5", {"name": "X"})[0], 403)   # editing off
        self.dash.manager = object()                                           # editing on
        status, g = self.post("/api/groups", {"name": "Night team", "enabled": True, "doors": {"1": 1}})
        self.assertEqual((status, g["id"], g["doors"]["1"]), (200, 3, 1))
        status, s = self.post("/api/time-slots", {"name": "Nights", "days": {"mon": ["20:00-23:00"]}})
        self.assertEqual((status, s["id"]), (200, 2))
        self.assertEqual(self.post("/api/time-slots/2", {"days": {"mon": ["22:00-02:00"]}})[0], 400)
        self.assertEqual(self.post("/api/groups", {"enabled": True})[0], 400)      # a new group needs a name


if __name__ == "__main__":
    unittest.main()
