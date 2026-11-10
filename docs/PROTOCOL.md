# Vauban VISOR+ / VERSO+ embedded web server: protocol notes

What visor-dash relies on, worked out from the controller's own web interface (the JavaScript it serves to browsers). Observed on a controller reporting firmware 3.8 with web pages 3.5. Other versions may differ; the dashboard checks the shapes below and refuses to write when they don't match.

## Transport

- Plain HTTP on port 80, **Digest authentication** (MD5, realm `protected`) on everything except `/`.
- The UI lives under `/FR/`, data files under `/assets/`. Static `.js`/`.css` files are stored gzip-compressed and served with `Content-Encoding: gzip`.
- The controller is a small embedded device: send one request at a time.

## A known failure: "Version du serveur web incompatible"

If the web pages stored on the controller come from a different firmware build than the one running, the controller still serves them but:

- injects `<h1 style='color:red;'>[strVersionServeurIncompatible]</h1>` into `/FR/menu.cgi` (in the middle of a script, which then fails: the UI stays on its loading screen);
- writes its dynamic values at the wrong byte offsets, leaving template tags such as `~charge_menu~` or `~charge_lecteur~` in the page.

`/FR/index.cgi` shows both versions: `var version='35',versionCentrale='38'`. The data files and most forms below keep working; door and automation settings pages come back without their values.

## Reading

| Request | Format |
|---|---|
| `GET /assets/Users` | Binary, one **49-byte record per user slot** (slot n = user index n). Bytes 0–6: identifier, little-endian (badge or code number). 7: identifier type (0 = empty slot, 1 proximity badge, 2 app badge, 3 remote, 4 PIN, 9 no identifier). 8–9 / 10–11 / 12–13: groups 1–3 (uint16 LE). 14–30: last name, 31–47: first name (UTF-8, NUL-padded, may end with a space). 48: status (0 authorised, 1 blocked). |
| `GET /assets/Groupes`, `Lecteurs`, `Plages`, `Lifts` | Labels separated by `#`, CR or LF, NUL-padded; entry n = id n; empty = default name. |
| `GET /assets/traductions-{fr,en,it}.js` | `key=value` lines (UTF-8 with BOM). Numeric keys are **event names** (`10=Access Granted`, `74=PSU fault On`…). |
| `GET /FR/GetEvenements.cgi?index=N` | Events separated by `#`, fields by `$`: `dd/mm/yyyy hh:mm:ss$code$reader$identifier$userIndex`. The last line is the next index to ask for. `index=0` = newest. |

### The event log is a ring

The log has **15,000 positions**. A page asked for at `index=X` holds positions X, X−1, … and, past position 1, continues at 15000, 14999… (the previous lap). Events are numbered by position, so a client can keep its own archive and fetch only what's new. When the log wraps, positions restart at 1. With a busy log (for example a power-supply fault logging every few seconds) the ring holds only a few days.

Reader numbers above 128 are the exit side of reader `n % 128`. User index 65535 on an access event means a deleted user. Event 0 ("unknown credential") carries the badge number of a badge the controller doesn't know, which is handy for enrolment.

## Forms

Edit pages (`*.cgi?…`) contain an HTML form with the fields and their defaults. The controller then injects JavaScript lines with the record's **non-default** values only:

```js
document.getElementById('lastname').value="DOE";
document.getElementById('authstatus').selectedIndex=1;
MajIdentifiant(1,'12345678');            // badge type and number
document.getElementById('isforbidden').checked=true;
```

Comparing the page with the blank form (`index=-1` / `id=-1`) isolates these lines.

Forms are saved with `POST /FR/update.cgi`, **exactly as the UI's `EnvoiPostAjax()` builds the body**: every control in document order, `name=value&`, checkboxes as `on`/`off`, only the checked radio, disabled controls included, and **values not URL-encoded** (UTF-8 bytes as-is). Characters like `&`, `=`, `#`, `$` must therefore be kept out of values.

The reply is a script ending in `Succes(…)` or `Erreur(n)`; the error texts are in the same script.

| Record | Page | Form type | Notes |
|---|---|---|---|
| User | `utilisateurs-creation.cgi?id=<slot, 5 digits>&type=<t>&ident=<number or ->` | `type=11` | `id=0` creates (the controller picks the first free slot). Badge number/type can't be changed after creation. Delete: `GET delete.cgi?type=11&index=<slot>`. |
| Group (1–1024) | `horaires-groupe.cgi?index=N` | `type=2` | Per door: `autorisationLecteurN` = 255 no access, 0 any time, or a time-slot number. `isforbidden` switches the group off. The door rows are added by `AjouteLecteur(n)` calls. |
| Time slot (1–128) | `horaires-plage.cgi?index=N` | `type=4` | `lundi1..10` … `dimanche1..10`, `jourferie1..10` (public holidays): `HH:MM-HH:MM` or empty. `typeAffichage` is display-only. |
| Public holiday | `horaires-jour-ferie.cgi?index=N` | `type=3` | Field `addjf`. Delete: `delete.cgi?type=03&index=N`. Not used by visor-dash. |

## Actions

| Request | Effect |
|---|---|
| `GET /FR/open.cgi?index=<reader>` | Opens a door briefly. Logs event 49 ("remote opening") at that reader. |
| `GET /FR/backup.cgi`, then `GET /BACKUP` | Builds and downloads the controller's backup file (starts with `CENTRALE`), the format its restore function accepts. |
| `POST /FR/upload.html?type=…` | Restore (1), user import (2), firmware (3–11), certificates (16). Not used by visor-dash. |
| `GET /FR/deleteAll.cgi` | Deletes every user. Not used by visor-dash. |
