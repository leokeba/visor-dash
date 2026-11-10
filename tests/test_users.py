"""Offline tests for user management (synthetic data only).  Run: python3 -m unittest -v"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from visor_dash.client import VaubanClient, parse_users
from visor_dash.server import Dashboard, make_handler
from visor_dash.users import (EXPECTED_FIELDS, FormError, UserChange, ValidationError, apply_change,
                              build_form, diff_users_file, interpret_response, record_matches_form)
from tests.test_dashboard import FakeClient, user_record

# A form page shaped like utilisateurs-creation.cgi (controls in the real order).
_CONTROLS = {
    "type": '<input type="hidden" name="type" id="type" value="11" />',
    "id": '<input type="hidden" name="id" id="id" value="0" />',
    "empreinte": '<input type="hidden" name="empreinte" id="empreinte" value="0" />',
    "ancienTypeIdentifiant": '<input type="hidden" name="ancienTypeIdentifiant" id="ancienTypeIdentifiant" value="9" />',
    "lastname": '<input type="text" name="lastname" id="lastname" maxlength="16" />',
    "firstname": '<input type="text" name="firstname" id="firstname" maxlength="16" />',
    "ident": '<input type="text" name="ident" id="ident" />',
    "typeIdentifiant": '<select id="typeIdentifiant" name="typeIdentifiant"><option value="1">[strBadgeProximite]</option>'
                       '<option value="2">b</option><option value="3">c</option><option value="4">d</option></select>',
    "presence": '<input type="text" id="presence" name="presence" readonly="readonly" value="[strIndeterminee]" />',
    "flagapb": '<input type="hidden" name="flagapb" id="flagapb" value="1" />',
    "razapb": '<input type="hidden" name="razapb" id="razapb" value="0" />',
    "authstatus": '<select name="authstatus" id="authstatus"><option value="0">a</option><option value="1">i</option></select>',
    "plage": '<select name="plage" id="plage"><option value="255">g</option><option value="0">p</option></select>',
    "authdates": '<input type="checkbox" id="authdates" name="authdates" />',
    "groupeAcces1": '<select name="groupeAcces1" id="groupeAcces1"><option value="0">n</option></select>',
    "groupeAcces2": '<select name="groupeAcces2" id="groupeAcces2"><option value="0">n</option></select>',
    "groupeAcces3": '<select name="groupeAcces3" id="groupeAcces3"><option value="0">n</option></select>',
    "codeSaisi": '<input type="number" name="codeSaisi" id="codeSaisi" min="1" value="1" />',
    "passrestantsvalue": '<input type="hidden" name="passrestantsvalue" id="passrestantsvalue" value="65535" />',
    "flagnbpass": '<input type="hidden" name="flagnbpass" id="flagnbpass" value="1" />',
    "listeModuleEnrolement": '<select id="listeModuleEnrolement" name="listeModuleEnrolement"></select>',
}
for _cb in ("alarm", "nocontrol", "cycleback", "modeOffice", "codeBadge", "nbpass"):
    _CONTROLS[_cb] = f'<input type="checkbox" id="{_cb}" name="{_cb}" />'
for _t in ("authdate1", "authdate1hour", "authdate1min", "authdate2", "authdate2hour", "authdate2min", "nbpassvalue"):
    _CONTROLS[_t] = f'<input type="text" name="{_t}" id="{_t}" />'

TEMPLATE_SCRIPT = ('<script type="text/javascript">\nfunction Chargement()\n{\n'
                   '\t\t\t\t\tvar id = document.getElementById("id").value;\n}\nChargement();\n</script>')


def page(injected=""):
    controls = "\n".join(_CONTROLS[n] + '\n<input type="button" value="[strX]" />' for n in EXPECTED_FIELDS)
    return (f'<form id="formUtilisateur" name="formUtilisateur" action="update.cgi" method="post">\n'
            f"{controls}\n</form>\n{TEMPLATE_SCRIPT.replace('{', '{' + chr(10) + injected, 1)}")


BLANK = {l.strip() for l in page().splitlines()}
GROUPS, SLOTS = [1, 2, 3, 4], [1]
TR = {"strIndeterminee": "Indéterminée", "strBadgeProximite": "Badge"}

EXISTING = "\n".join([
    "document.getElementById('id').value=7;",
    "document.getElementById('flagnbpass').value=1;",
    "document.getElementById('flagapb').value=2;",
    'document.getElementById(\'lastname\').value="DUPONT ";',
    'document.getElementById(\'firstname\').value="Jean";',
    'document.getElementById(\'ancienTypeIdentifiant\').value="1";',
    "MajIdentifiant(1,'40000001');",
    "document.getElementById('authstatus').selectedIndex=1;",
    "document.getElementById('groupeAcces1').selectedIndex = 2;",
    "document.getElementById('plage').value=1;",
    "document.getElementById('authdates').checked=true;",
    'document.getElementById(\'authdate1\').value="20/12/2022";',
    'document.getElementById(\'authdate1hour\').value="12";',
    'document.getElementById(\'authdate1min\').value="15";',
])


class FormTests(unittest.TestCase):
    def test_blank_form_body_matches_browser(self):
        f = build_form(page(), BLANK, GROUPS, SLOTS, TR)
        body = f.body().decode()
        self.assertTrue(body.startswith("type=11&id=0&empreinte=0&ancienTypeIdentifiant=9&lastname=&"))
        self.assertIn("&presence=Indéterminée&", body)          # translated, not percent-encoded
        self.assertIn("&authdates=off&", body)
        self.assertIn("&plage=255&", body)
        self.assertTrue(body.endswith("&listeModuleEnrolement=&"))
        self.assertEqual([kv.split("=")[0] for kv in body.rstrip("&").split("&")], EXPECTED_FIELDS)

    def test_existing_user_statements(self):
        f = build_form(page(EXISTING), BLANK, GROUPS, SLOTS, TR)
        v = f.values()
        self.assertEqual((v["id"], v["lastname"], v["ident"], v["typeIdentifiant"]), ("7", "DUPONT ", "40000001", "1"))
        self.assertEqual((v["authstatus"], v["groupeAcces1"], v["plage"], v["flagapb"]), ("1", "2", "1", "2"))
        self.assertEqual((v["authdates"], v["authdate1"], v["authdate1hour"]), ("on", "20/12/2022", "12"))
        u = parse_users(user_record(40000001, 1, (2, 0, 0), "DUPONT ", "Jean", 1))[0]
        self.assertEqual(record_matches_form(u, f), [])

    def test_unknown_statement_refused(self):
        with self.assertRaises(FormError):
            build_form(page(EXISTING + "\ndocument.getElementById('newField').value=3;"), BLANK, GROUPS, SLOTS, TR)
        with self.assertRaises(FormError):
            build_form(page("SomethingNew(1);"), BLANK, GROUPS, SLOTS, TR)

    def test_changed_form_refused(self):
        html = page().replace(_CONTROLS["alarm"], "")
        with self.assertRaises(FormError):
            build_form(html, BLANK, GROUPS, SLOTS, TR)

    def test_out_of_range_index_refused(self):
        with self.assertRaises(FormError):
            build_form(page("document.getElementById('groupeAcces1').selectedIndex = 99;"), BLANK, GROUPS, SLOTS, TR)


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.users = parse_users(user_record(555, 1, (1, 0, 0), "A", "B", 0))

    def form(self, injected=""):
        return build_form(page(injected), BLANK, GROUPS, SLOTS, TR)

    def test_create_sets_fields(self):
        f = self.form()
        apply_change(f, UserChange(last_name="Élodie", first_name="Test", identifier="123", id_type=1,
                                   authorised=False, groups=(1, 3, 0), time_slot=0,
                                   valid_from="01/01/2027 08:00", valid_until="31/12/2027 18:30"),
                     True, self.users, None, GROUPS, SLOTS)
        v = f.values()
        self.assertEqual((v["lastname"], v["ident"], v["authstatus"], v["groupeAcces2"], v["plage"]),
                         ("Élodie", "123", "1", "3", "0"))
        self.assertEqual((v["authdates"], v["authdate1"], v["authdate1hour"], v["authdate2min"]),
                         ("on", "01/01/2027", "8", "30"))

    def test_validation(self):
        bad = [
            UserChange(last_name="A&B", identifier="1"),                 # separator characters
            UserChange(last_name="x" * 17, identifier="1"),              # too long
            UserChange(last_name="ééééééééé", identifier="1"),           # 18 bytes
            UserChange(last_name="X", identifier="555"),                 # duplicate badge
            UserChange(last_name="X", identifier="0"),
            UserChange(last_name="X", identifier="12a"),
            UserChange(last_name="X", identifier="1", groups=(9, 0, 0)), # unknown group
            UserChange(last_name="X", identifier="1", time_slot=42),
            UserChange(last_name="X", identifier="1", valid_from="01/01/2027 08:00", valid_until=""),
            UserChange(last_name="X", identifier="1", valid_from="02/01/2027 08:00", valid_until="01/01/2027 08:00"),
            UserChange(last_name="", first_name="", identifier="1"),
        ]
        for ch in bad:
            with self.assertRaises(ValidationError, msg=repr(ch)):
                apply_change(self.form(), ch, True, self.users, None, GROUPS, SLOTS)

    def test_edit_cannot_change_badge(self):
        with self.assertRaises(ValidationError):
            apply_change(self.form(EXISTING), UserChange(identifier="999"), False, self.users, 7, GROUPS, SLOTS)

    def test_edit_keeps_untouched_fields(self):
        f = self.form(EXISTING)
        before = f.values()
        apply_change(f, UserChange(first_name="Testfirst"), False, self.users, 7, GROUPS, SLOTS)
        after = f.values()
        self.assertEqual({k for k in before if before[k] != after[k]}, {"firstname"})

    def test_clear_validity(self):
        f = self.form(EXISTING)
        apply_change(f, UserChange(valid_from="", valid_until=""), False, self.users, 7, GROUPS, SLOTS)
        self.assertEqual((f.get("authdates"), f.get("authdate1")), ("off", ""))


class ResponseAndDiffTests(unittest.TestCase):
    def test_interpret(self):
        script = ('﻿<script>\nfunction Erreur(t)\n{\nvar message = \'[strEchecMiseAJour]\';\nswitch(t)\n{\n'
                  'case 1: // droits\n\t\tmessage = "[strErreurDroits]";\nbreak;\n}\n}\n%s\n</script>')
        tr = {"strErreurDroits": "Insufficient rights", "strEchecMiseAJour": "Update failed"}
        self.assertEqual(interpret_response(script % "Erreur(1);", tr), (False, "Insufficient rights (code 1)"))
        self.assertEqual(interpret_response(script % "Erreur(5);", tr), (False, "Update failed (code 5)"))
        self.assertTrue(interpret_response(script % "Succes();", tr)[0])

    def test_diff(self):
        a = user_record(1, 1, (0, 0, 0), "A", "", 0) + user_record(2, 1, (0, 0, 0), "B", "", 0)
        b = user_record(1, 1, (0, 0, 0), "A", "", 0) + user_record(2, 1, (0, 0, 0), "C", "", 0) + user_record(3, 1, (0, 0, 0), "D", "", 0)
        self.assertEqual(diff_users_file(a, b), [2, 3])


class ClientGuardTests(unittest.TestCase):
    def test_writes_off_by_default(self):
        c = VaubanClient("192.0.2.1", "u", "p")
        with self.assertRaises(PermissionError):
            c.post_form("/FR/update.cgi", b"type=11&id=0&")
        with self.assertRaises(PermissionError):
            c.get_action("/FR/delete.cgi", "type=11&index=5")

    def test_only_user_writes(self):
        c = VaubanClient("192.0.2.1", "u", "p", allow_writes=True)
        for path, body in (("/FR/update.cgi", b"type=6&id=1&"), ("/FR/upload.html", b"type=11&id=0&")):
            with self.assertRaises(PermissionError):
                c.post_form(path, body)
        for q in ("type=01&index=5", "type=11&index=0", "type=11&index=5&x=1", "type=11&index=-1"):
            with self.assertRaises(PermissionError, msg=q):
                c.get_action("/FR/delete.cgi", q)
        with self.assertRaises(PermissionError):
            c.get_action("/FR/deleteAll.cgi", "")


class FakeManager:
    def __init__(self):
        self.calls = []

    def create(self, ch):
        self.calls.append(("create", ch))
        return {"slot": 9}

    def update(self, slot, ch):
        self.calls.append(("update", slot, ch))
        return {"slot": slot, "changed": True}

    def delete(self, slot):
        self.calls.append(("delete", slot))
        return {"slot": slot, "deleted": True}


class HttpGuardTests(unittest.TestCase):
    def setUp(self):
        self.dash = Dashboard(FakeClient(), "en")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dash, None, None))
        port = self.httpd.server_address[1]
        # Same Host check as a localhost-bound dashboard.
        self.httpd.RequestHandlerClass = make_handler(self.dash, None, {f"127.0.0.1:{port}"})
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Dashboard": "1"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method=method,
                                     headers={k: v for k, v in h.items() if v is not None})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_writes_disabled(self):
        self.assertEqual(self.call("POST", "/api/people", {"last_name": "X"})[0], 403)

    def test_guards_and_happy_path(self):
        self.dash.manager = FakeManager()
        self.assertEqual(self.call("POST", "/api/people", {"last_name": "X"}, {"X-Dashboard": None})[0], 403)
        self.assertEqual(self.call("POST", "/api/people", {}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/people", {}, {"Host": "evil.example"})[0], 421)
        self.assertEqual(self.call("POST", "/api/people", {"groups": [1, 2]})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/people/5", {"confirm_slot": 6})[0], 400)
        status, body = self.call("POST", "/api/people", {"last_name": "X", "identifier": "12", "groups": [1, 0, 0]})
        self.assertEqual((status, body), (200, {"slot": 9}))
        self.assertEqual(self.call("POST", "/api/people/5", {"first_name": "Y"})[0], 200)
        self.assertEqual(self.call("DELETE", "/api/people/5", {"confirm_slot": 5})[0], 200)
        self.assertEqual([c[0] for c in self.dash.manager.calls], ["create", "update", "delete"])


if __name__ == "__main__":
    unittest.main()
