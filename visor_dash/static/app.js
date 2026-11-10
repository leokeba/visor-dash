"use strict";
// Badge dashboard front end. All controller data is inserted with textContent, never as HTML.

const REFRESH_MS = 15000;
const OVERVIEW_MS = 60000;
const PEOPLE_MS = 300000;
const ACTIVITY_PAGE = 200;
const SEARCH_DELAY_MS = 300;

const state = {
  events: new Map(),        // event number -> event
  doors: [],
  people: null,
  peopleLoadedAt: 0,
  activityFilter: "access",
  activity: { rows: new Map(), hasMore: false, req: 0, archive: null, importInfo: null },
  historySince: null,
  options: null,
  peopleFilter: "all",
  oldestNo: null,
  lastOk: null,
};

// Plain-language guidance for alerts, keyed by event code.
const ALERT_HELP = {
  74: "The controller keeps reporting a power-supply fault. Check the power supply unit, its mains wiring and the backup battery.",
  59: "The backup battery is low. Replace it so the doors keep working during a power cut.",
  40: "A door was opened without a valid badge or button.",
  41: "A door stayed open or blocked for too long.",
  53: "A reader sent a badge it could not decode. A few are normal; many in a row point to a reader or wiring fault.",
  58: "A badge reader lost its connection to the controller.",
  61: "A wireless lock lost its connection.",
  63: "Radio interference on the wireless locks.",
  67: "Someone tampered with a badge reader.",
  72: "The controller's case was opened.",
  78: "An extension module lost its connection.",
  87: "The controller restarted. Expected after a power cut or an update; otherwise worth checking.",
  200: "The controller is overheating.",
};
const CLEARED_BY = { 74: 75, 40: 42, 41: 43, 67: 68, 72: 73, 58: 57, 78: 77, 59: 60, 61: 62, 63: 64 };

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

async function api(path) {
  const res = await fetch(path, { cache: "no-store" });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new Error((body && body.error) || `The dashboard server answered ${res.status}.`);
  return body;
}

const pad = (n) => String(n).padStart(2, "0");
// Unambiguous everywhere ("7 Nov 2026"), unlike 11/7/2026.
const dayText = (d) => d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
const parseIso = (iso) => (iso ? new Date(iso) : null);
const isToday = (d) => { const n = new Date(); return d && d.toDateString() === n.toDateString(); };
const clock = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function ago(d) {
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  return isToday(d) ? `at ${clock(d)}` : `${dayText(d)} ${clock(d)}`;
}

function showError(msg) {
  const e = $("error");
  e.textContent = msg || "";
  e.hidden = !msg;
  const c = $("conn");
  if (msg) {
    c.className = "conn down";
    c.textContent = state.lastOk ? `Can't reach the controller. Last update ${clock(state.lastOk)}.` : "Can't reach the controller.";
  }
}

function markOk() {
  state.lastOk = new Date();
  showError(null);
  const c = $("conn");
  c.className = "conn ok";
  c.textContent = `Live, updated ${clock(state.lastOk)}:${pad(state.lastOk.getSeconds())}`;
}

function sortedEvents() {
  return [...state.events.values()].sort((a, b) => b.no - a.no);
}

function todaysEvents() {
  return sortedEvents().filter((e) => isToday(parseIso(e.iso)));
}

// ---------------------------------------------------------------- overview
function renderOverview(o) {
  document.title = `${o.site_name} – badge dashboard`;
  $("site-name").textContent = o.site_name;
  state.doors = o.doors;
  renderBackup(o.backup);
  const v = o.versions || {};
  const notice = $("version-notice");
  if (v.web_server && v.controller && v.web_server !== v.controller) {
    notice.textContent = `The controller's built-in web pages (v${v.web_server}) don't match its firmware (v${v.controller}), which is why they don't load. This dashboard reads the data directly, so it isn't affected.`;
    notice.hidden = false;
  } else {
    notice.hidden = true;
  }
}

function whenText(iso) {
  const d = parseIso(iso);
  if (!d) return "unknown";
  const yesterday = new Date(Date.now() - 86400000);
  if (isToday(d)) return `today at ${clock(d)}`;
  if (d.toDateString() === yesterday.toDateString()) return `yesterday at ${clock(d)}`;
  return `${dayText(d)} at ${clock(d)}`;
}

function renderBackup(b) {
  const notice = $("backup-notice");
  const foot = $("foot-backup");
  if (!b) { notice.hidden = true; foot.textContent = ""; return; }
  const last = b.last_success ? whenText(b.last_success) : null;
  foot.textContent = last ? `Last backup ${last}.` : "";
  let msg = "";
  if (b.state === "none") {
    msg = "No automatic backup yet. Run “python3 -m visor_dash backup” or install the service (see README).";
  } else if (b.state === "failed") {
    msg = `The last backup failed (${b.last_error || "unknown error"}).` + (last ? ` The last good one is from ${last}.` : "");
  } else if (b.state === "stale") {
    msg = `The last good backup is from ${last}. Check that the nightly backup job is running.`;
  } else if (b.problems) {
    msg = `The last backup skipped ${b.problems} person(s) it could not read. See backups/last-backup.json.`;
  }
  notice.textContent = msg;
  notice.hidden = !msg;
}

// ---------------------------------------------------------------- day ribbon
function minutesOfDay(d) { return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; }

function renderHours() {
  const hours = $("hours");
  hours.replaceChildren();
  for (let h = 0; h <= 24; h += 3) {
    const s = el("span", { text: `${pad(h)}h` });
    s.style.left = `${(h / 24) * 100}%`;
    hours.append(s);
  }
}

function renderRibbon(events) {
  const today = new Date();
  $("day-date").textContent = today.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const nowPct = (minutesOfDay(today) / 1440) * 100;
  for (const cat of ["granted", "refused", "alarm"]) {
    const lane = document.querySelector(`.lane[data-cat="${cat}"]`);
    const track = lane.querySelector(".track");
    const items = events.filter((e) => e.category === cat);
    $(`count-${cat}`).textContent = String(items.length);
    const frag = document.createDocumentFragment();
    for (const e of items) {
      const d = parseIso(e.iso);
      const t = el("span", { class: "tick", "data-no": String(e.no) });
      t.style.left = `${(minutesOfDay(d) / 1440) * 100}%`;
      frag.append(t);
    }
    const now = el("span", { class: "now" });
    now.style.left = `${nowPct}%`;
    frag.append(now);
    track.replaceChildren(frag);
  }
}

function describe(e) {
  const bits = [e.label];
  if (e.door) bits.push(e.door);
  if (e.person) bits.push(e.person);
  else if (e.identifier) bits.push(`Badge ${e.identifier}`);
  if (e.detail) bits.push(e.detail);
  return bits;
}

function setupTooltip() {
  const tip = $("tip");
  const ribbon = $("ribbon");
  const show = (target, x, y) => {
    const e = state.events.get(Number(target.dataset.no));
    if (!e) return;
    const d = parseIso(e.iso);
    const [title, ...rest] = describe(e);
    tip.replaceChildren(el("strong", { text: `${clock(d)}:${pad(d.getSeconds())}  ${title}` }), rest.join(", "));
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = `${Math.min(Math.max(8, x - r.width / 2), window.innerWidth - r.width - 8)}px`;
    tip.style.top = `${Math.max(8, y - r.height - 14)}px`;
  };
  ribbon.addEventListener("mouseover", (ev) => {
    if (ev.target.classList.contains("tick")) show(ev.target, ev.clientX, ev.clientY);
  });
  ribbon.addEventListener("mousemove", (ev) => {
    if (ev.target.classList.contains("tick")) show(ev.target, ev.clientX, ev.clientY);
  });
  ribbon.addEventListener("mouseout", (ev) => {
    if (ev.target.classList.contains("tick")) tip.hidden = true;
  });
  ribbon.addEventListener("click", (ev) => {
    if (ev.target.classList.contains("tick")) show(ev.target, ev.clientX, ev.clientY);
    else tip.hidden = true;
  });
  window.addEventListener("scroll", () => { tip.hidden = true; }, { passive: true });
}

// ---------------------------------------------------------------- doors
function renderDoors(events) {
  const list = $("doors");
  if (!state.doors.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No named doors on this controller." }));
    return;
  }
  const items = state.doors.map((door) => {
    const atDoor = events.filter((e) => e.door === door.name || e.door.startsWith(door.name + " "));
    const uses = atDoor.filter((e) => e.category === "granted" || e.category === "refused");
    const granted = uses.filter((e) => e.category === "granted").length;
    const refused = uses.length - granted;
    const last = uses[0];
    let lastLine;
    if (last) {
      const who = last.person || (last.identifier ? `badge ${last.identifier}` : "");
      const what = who ? `${last.label}, ${who}` : last.label;
      lastLine = el("p", { class: "door-last" },
        el("span", { class: `mark ${last.category}`, "aria-hidden": "true" }),
        `${what} `, el("span", { class: "when", text: ago(parseIso(last.iso)) }));
    } else {
      lastLine = el("p", { class: "door-last" }, el("span", { class: "when", text: "No one has used this door today." }));
    }
    const opened = `${granted} ${granted === 1 ? "opening" : "openings"}`;
    const today = refused ? `${opened}, ${refused} refused today` : `${opened} today`;
    return el("li", { class: "door" },
      el("span", { class: "door-name" }, el("button", { type: "button", class: "door-link", "data-door": String(door.id), text: door.name })),
      el("span", { class: "door-today", text: today }),
      lastLine);
  });
  list.replaceChildren(...items);
}

// ---------------------------------------------------------------- alerts
function renderAlerts(events) {
  const byCode = new Map();
  for (const e of events) {
    if (e.category !== "alarm" && e.code !== 87) continue;
    if (!byCode.has(e.code)) byCode.set(e.code, []);
    byCode.get(e.code).push(e);
  }
  const list = $("alerts");
  if (!byCode.size) {
    list.replaceChildren(el("li", { class: "empty", text: "No alerts since midnight." }));
    return;
  }
  const groups = [...byCode.entries()].sort((a, b) => b[1].length - a[1].length);
  list.replaceChildren(...groups.map(([code, evs]) => {
    const latest = evs[0];
    const latestDate = parseIso(latest.iso);
    const clearCode = CLEARED_BY[code];
    let status = `Last ${clock(latestDate)}`;
    if (clearCode != null) {
      const cleared = events.find((e) => e.code === clearCode);
      const active = !cleared || cleared.no < latest.no;
      status = active ? "Active now" : `Cleared at ${clock(parseIso(cleared.iso))}`;
    }
    if (evs.length > 1) {
      status += `${clearCode != null ? "." : ""} First at ${clock(parseIso(evs[evs.length - 1].iso))}, latest at ${clock(latestDate)}`;
    } else if (clearCode != null) {
      status += `. Reported at ${clock(latestDate)}`;
    }
    const help = ALERT_HELP[code] || "";
    const times = evs.length === 1 ? "once" : `${evs.length} times`;
    return el("li", { class: "alert" },
      el("div", { class: "alert-title" }, el("span", { text: latest.label }), el("span", { class: "alert-count", text: times })),
      el("p", { text: status + (latest.door ? `, ${latest.door}` : "") + "." }),
      help ? el("p", { text: help }) : null);
  }));
}

// ---------------------------------------------------------------- activity (long-term history)
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function activityParams() {
  const params = new URLSearchParams({ filter: state.activityFilter });
  const q = $("activity-search").value.trim();
  if (q) params.set("q", q);
  const period = $("activity-period").value;
  if (period !== "all") {
    const d = new Date();
    if (period !== "today") d.setDate(d.getDate() - (Number(period) - 1));
    params.set("from", ymd(d));
  }
  return params;
}

// mode: "reset" (filters changed), "older" (next page), "refresh" (pick up new events)
async function loadActivity(mode) {
  const a = state.activity;
  const req = mode === "reset" ? ++a.req : a.req;
  const params = activityParams();
  params.set("limit", String(ACTIVITY_PAGE));
  if (mode === "older") {
    const keys = [...a.rows.keys()];
    if (keys.length) params.set("before", String(Math.min(...keys)));
  }
  const data = await api(`/api/history?${params}`);
  if (req !== a.req) return;            // filters changed while this was loading
  if (mode === "reset") a.rows.clear();
  const wasEmpty = a.rows.size === 0;
  for (const e of data.events) a.rows.set(e.no, e);
  if (mode !== "refresh" || wasEmpty) a.hasMore = data.has_more;
  a.archive = data.archive;
  a.importInfo = data.import;
  $("export-csv").href = `/api/history.csv?${activityParams()}`;
  renderActivity();
}

function shortDate(iso) {
  const d = parseIso(iso);
  return d ? dayText(d) : "";
}

function renderActivity() {
  const a = state.activity;
  const rows = [...a.rows.values()].sort((x, y) => y.no - x.no);
  const body = $("activity-rows");
  if (!rows.length) {
    const msg = $("activity-search").value.trim()
      ? "Nothing matches that search in this period. Try a longer period."
      : "No events of this kind in this period.";
    body.replaceChildren(el("tr", null, el("td", { class: "none", colspan: "5", text: msg })));
  } else {
    body.replaceChildren(...rows.map((e) => {
      const d = parseIso(e.iso);
      const time = el("td", { class: "time" });
      if (d && !isToday(d)) time.append(el("span", { class: "day-part", text: `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ` }));
      time.append(d ? `${clock(d)}:${pad(d.getSeconds())}` : e.date);
      return el("tr", null,
        time,
        el("td", null, el("span", { class: `mark ${e.category}`, "aria-hidden": "true" }), e.label,
          e.detail ? el("span", { class: "detail", text: e.detail }) : null),
        el("td", { text: e.door }),
        el("td", { class: "badge", text: e.identifier }),
        el("td", null, e.person, enrolButton(e)));
    }));
  }
  const bits = [`${rows.length} event${rows.length === 1 ? "" : "s"} shown.`];
  if (a.archive && a.archive.oldest) {
    const keep = a.archive.retention_days ? ` Events are kept for ${a.archive.retention_days} days.` : "";
    bits.push(`History starts ${shortDate(a.archive.oldest)}.${keep}`);
  }
  if (a.importInfo && a.importInfo.running) bits.push("Still importing older events from the controller…");
  $("activity-meta").textContent = bits.join(" ");
  $("load-older").hidden = !a.hasMore;
}

const UNKNOWN_BADGE = 0;
function enrolButton(e) {
  if (e.code !== UNKNOWN_BADGE || !e.identifier || !state.options || !state.options.writes_enabled) return null;
  if (state.people && state.people.some((p) => p.identifier === e.identifier)) return null;  // added since
  return el("button", { type: "button", class: "row-action", "data-ident": e.identifier, text: "Add as person" });
}

// ---------------------------------------------------------------- people table
function seenText(iso) {
  const d = parseIso(iso);
  if (!d) return "–";
  const yesterday = new Date(Date.now() - 86400000);
  if (isToday(d)) return `today ${clock(d)}`;
  if (d.toDateString() === yesterday.toDateString()) return `yesterday ${clock(d)}`;
  return dayText(d);
}

function sortPeople(list) {
  const mode = $("people-sort").value;
  if (mode === "name") return list;
  const t = (p) => (p.last_seen ? Date.parse(p.last_seen) : null);
  return [...list].sort((a, b) => {
    const ta = t(a), tb = t(b);
    if (ta === tb) return 0;
    if (ta === null) return mode === "seen-old" ? -1 : 1;   // never seen: first when looking for inactive people
    if (tb === null) return mode === "seen-old" ? 1 : -1;
    return mode === "seen-old" ? ta - tb : tb - ta;
  });
}

function renderPeople() {
  if (!state.people) return;
  const q = $("people-search").value.trim().toLowerCase();
  const rows = sortPeople(state.people).filter((p) => {
    if (state.peopleFilter !== "all" && p.status !== state.peopleFilter) return false;
    if (!q) return true;
    return [p.last_name, p.first_name, p.identifier, ...p.groups].some((s) => s && s.toLowerCase().includes(q));
  });
  const body = $("people-rows");
  if (!rows.length) {
    body.replaceChildren(el("tr", null, el("td", { class: "none", colspan: "5", text: "No one matches. Check the spelling or switch to Everyone." })));
  } else {
    body.replaceChildren(...rows.map((p) => {
      const name = [p.last_name, p.first_name].filter(Boolean).join(" ") || `Unnamed (slot ${p.index})`;
      const access = p.status === "authorised" ? "Authorised" : p.status === "forbidden" ? "Blocked" : p.status;
      return el("tr", null,
        el("td", null, el("button", { type: "button", class: "person-link", "data-slot": String(p.index), text: name })),
        el("td", { class: "badge", text: p.identifier || "–" }),
        el("td", { text: p.groups.join(", ") }),
        el("td", { class: `status-${p.status}`, text: access }),
        el("td", { class: p.last_seen ? "seen" : "seen never", text: seenText(p.last_seen),
                   title: p.last_seen ? (p.last_door ? `at ${p.last_door}` : null) : "No badge use recorded in the history" }));
    }));
  }
  const authorised = state.people.filter((p) => p.status === "authorised").length;
  $("people-count").textContent = String(state.people.length);
  const since = state.historySince ? ` "Last seen" counts badge use since ${shortDate(state.historySince)}.` : "";
  $("people-meta").textContent = `${rows.length} shown. ${authorised} of ${state.people.length} people are authorised; the rest are blocked.${since}`;
}

async function loadPeople(force) {
  if (!force && state.people && Date.now() - state.peopleLoadedAt < PEOPLE_MS) return;
  const data = await api("/api/people");
  state.historySince = data.history_since;
  const named = (p) => (p.last_name || p.first_name ? 0 : 1);  // unnamed slots go last
  state.people = data.people.sort((a, b) =>
    named(a) - named(b)
    || a.last_name.localeCompare(b.last_name, undefined, { sensitivity: "base" })
    || a.first_name.localeCompare(b.first_name, undefined, { sensitivity: "base" })
    || a.index - b.index);
  state.peopleLoadedAt = Date.now();
  renderPeople();
  renderActivity();   // hides "Add as person" for badges that now belong to someone
}

// ---------------------------------------------------------------- refresh loop
let refreshing = false;
async function refresh(force) {
  if (refreshing) return;
  refreshing = true;
  $("refresh").disabled = true;
  try {
    const today = await api(`/api/today${force ? "?refresh=1" : ""}`);
    state.events = new Map(today.events.map((e) => [e.no, e]));   // only today's, for the ribbon
    const events = todaysEvents();
    renderRibbon(events);
    renderDoors(events);
    renderAlerts(events);
    await loadActivity("refresh");
    markOk();
  } catch (err) {
    showError(err.message);
  } finally {
    refreshing = false;
    $("refresh").disabled = false;
  }
}

async function refreshOverview() {
  try {
    renderOverview(await api("/api/overview"));
  } catch (err) {
    showError(err.message);
  }
}

async function loadOlder() {
  const btn = $("load-older");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    await loadActivity("older");
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load older events";
  }
}


// ---------------------------------------------------------------- person dialog (view / add / edit / delete)
let editing = null;   // { slot: number|null, data: object|null }

async function apiWrite(method, path, body) {
  const res = await fetch(path, {
    method,
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `The dashboard server answered ${res.status}.`);
  return data;
}

async function loadOptions() {
  state.options = await api("/api/options");
  const writable = state.options.writes_enabled;
  $("add-person").hidden = !writable;
  $("foot-mode").textContent = writable
    ? "Editing is on. Every change is checked against the controller and logged in backups/audit.jsonl."
    : "This page only reads from the controller. It can't change badges or open doors.";
  renderActivity();   // "Add as person" buttons depend on whether editing is on
}

function fillSelect(select, items, current, missingLabel) {
  const opts = items.map((it) => el("option", { value: String(it.id), text: it.name }));
  if (current != null && !items.some((it) => it.id === current)) {
    opts.push(el("option", { value: String(current), text: missingLabel(current) }));
  }
  select.replaceChildren(...opts);
  if (current != null) select.value = String(current);
}

const pad2 = (n) => String(n).padStart(2, "0");
function toInputDate(s) {          // "31/12/2026 18:00" -> "2026-12-31T18:00"
  const m = /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2})$/.exec(s || "");
  return m ? `${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}` : "";
}
function fromInputDate(s) {        // "2026-12-31T18:00" -> "31/12/2026 18:00"
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : "";
}
const utf8Length = (s) => new TextEncoder().encode(s).length;

function setPersonError(msg) {
  const e = $("person-error");
  e.textContent = msg || "";
  e.hidden = !msg;
}

function showConfirm(on) {
  $("person-actions").hidden = on;
  $("person-confirm").hidden = !on;
}

function populatePerson(d, isNew) {
  const o = state.options;
  const writable = o.writes_enabled;
  const groupItems = [{ id: 0, name: "None" }, ...o.groups];
  const g = d ? d.groups : [0, 0, 0];
  ["f-g1", "f-g2", "f-g3"].forEach((id, i) => fillSelect($(id), groupItems, g[i], (n) => `Group ${n}`));
  fillSelect($("f-slot"), o.time_slots, d ? d.time_slot : 255,
    (n) => o.all_time_slots[String(n)] || `Time slot ${n}`);
  fillSelect($("f-type"), o.id_types, d ? d.id_type : 1, (n) => `Type ${n}`);

  $("f-last").value = d ? d.last_name.trim() : "";
  $("f-first").value = d ? d.first_name.trim() : "";
  $("f-ident").value = d ? d.identifier : "";
  for (const r of document.querySelectorAll('input[name="f-access"]')) {
    r.checked = r.value === (d ? (d.authorised ? "1" : "0") : "1");
  }
  $("f-valid").checked = !!(d && d.valid);
  $("f-from").value = d ? toInputDate(d.valid_from) : "";
  $("f-until").value = d ? toInputDate(d.valid_until) : "";
  $("f-dates").hidden = !$("f-valid").checked;
  $("other-hint").hidden = !(d && d.has_other_settings);

  // Badge number/type only editable when adding, like the controller's own UI.
  $("f-ident").disabled = !isNew || !writable;
  $("f-type").disabled = !isNew || !writable;
  $("badge-hint").hidden = isNew || !writable;
  $("read-badge").hidden = !isNew || !writable;
  stopSwipe();
  setSwipeStatus("");
  for (const c of $("person-form").querySelectorAll(".fields input, .fields select")) {
    if (c.id !== "f-ident" && c.id !== "f-type") c.disabled = !writable;
  }
  $("person-readonly").hidden = writable;
  $("person-save").hidden = !writable;
  $("person-save").textContent = isNew ? "Add person" : "Save changes";
  $("person-delete").hidden = !writable || isNew;
  $("person-cancel").textContent = writable ? "Cancel" : "Close";
  showConfirm(false);
  setPersonError(null);
}

async function openPerson(slot) {
  const dlg = $("person");
  try {
    if (!state.options) await loadOptions();
    const d = await api(`/api/people/${slot}`);
    editing = { slot, data: d };
    const name = [d.last_name.trim(), d.first_name.trim()].filter(Boolean).join(" ") || `Unnamed (slot ${slot})`;
    $("person-title").textContent = name;
    $("person-sub").textContent = d.identifier ? `Badge ${d.identifier}, slot ${slot}` : `No badge, slot ${slot}`;
    const h = d.history;
    const hist = $("person-history");
    if (h) {
      const where = h.last_door ? ` at ${h.last_door}` : "";
      const seen = h.last_seen ? `Last seen ${whenText(h.last_seen)}${where}.` : "No badge use recorded in the history.";
      const refused = h.refused ? `, ${h.refused} refused` : "";
      hist.textContent = `${seen} ${h.entries} entr${h.entries === 1 ? "y" : "ies"}${refused} in the last ${h.days} days.`;
    }
    hist.hidden = !h;
    $("person-recent").hidden = !d.recent;
    if (d.recent) {
      renderMiniEvents($("person-events"), d.recent, "door", "No badge use recorded in the history.");
      $("person-all").onclick = () => showInActivity(d.identifier);
    }
    populatePerson(d, false);
    if (!d.consistent) {
      setPersonError(`The controller's data for this person is inconsistent (${d.inconsistencies.join(", ")}), so it can't be edited here.`);
      $("person-save").hidden = true;
      $("person-delete").hidden = true;
    }
    dlg.showModal();
    $("f-last").focus();
  } catch (err) {
    showError(err.message);
  }
}

async function openNewPerson(prefillIdent) {
  try {
    if (!state.options) await loadOptions();
    editing = { slot: null, data: null };
    $("person-title").textContent = "Add a person";
    $("person-sub").textContent = "They are added to the controller as soon as you save.";
    $("person-history").hidden = true;
    $("person-recent").hidden = true;
    populatePerson(null, true);
    if (typeof prefillIdent === "string") {
      $("f-ident").value = prefillIdent;
      setSwipeStatus(`Badge ${prefillIdent} from the activity log.`, true);
    }
    $("person").showModal();
    $("f-last").focus();
  } catch (err) {
    showError(err.message);
  }
}

function readPersonForm(isNew) {
  const last = $("f-last").value.trim();
  const first = $("f-first").value.trim();
  if (!last && !first) throw new Error("Give at least a last name or a first name.");
  for (const [label, v] of [["Last name", last], ["First name", first]]) {
    if (utf8Length(v) > 16) throw new Error(`${label} is too long for the controller (16 bytes; accented letters count double).`);
  }
  const body = {
    last_name: last,
    first_name: first,
    authorised: document.querySelector('input[name="f-access"]:checked').value === "1",
    groups: ["f-g1", "f-g2", "f-g3"].map((id) => Number($(id).value)),
    time_slot: Number($("f-slot").value),
  };
  if ($("f-valid").checked) {
    body.valid_from = fromInputDate($("f-from").value);
    body.valid_until = fromInputDate($("f-until").value);
    if (!body.valid_from || !body.valid_until) throw new Error("Give both a start and an end for the validity period.");
  } else {
    body.valid_from = "";
    body.valid_until = "";
  }
  if (isNew) {
    const ident = $("f-ident").value.trim();
    if (!/^[0-9]{1,17}$/.test(ident) || /^0+$/.test(ident)) throw new Error("Badge number must be 1 to 17 digits and not 0.");
    body.identifier = ident;
    body.id_type = Number($("f-type").value);
  }
  return body;
}

function flash(msg) {
  const f = $("people-flash");
  f.textContent = msg;
  f.hidden = false;
}

async function savePerson(ev) {
  ev.preventDefault();
  if (!editing || !state.options.writes_enabled) return;
  const isNew = editing.slot == null;
  const btn = $("person-save");
  let body;
  try {
    body = readPersonForm(isNew);
  } catch (err) {
    setPersonError(err.message);
    return;
  }
  setPersonError(null);
  btn.disabled = true;
  btn.textContent = isNew ? "Adding…" : "Saving…";
  try {
    const name = [body.last_name, body.first_name].filter(Boolean).join(" ");
    if (isNew) {
      const r = await apiWrite("POST", "/api/people", body);
      flash(`Added ${name} (slot ${r.slot}).`);
    } else {
      const r = await apiWrite("POST", `/api/people/${editing.slot}`, body);
      flash(r.changed ? `Saved changes to ${name}.` : `No changes to save for ${name}.`);
    }
    $("person").close();
    await loadPeople(true);
  } catch (err) {
    setPersonError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = isNew ? "Add person" : "Save changes";
  }
}

function askDelete() {
  const d = editing && editing.data;
  if (!d) return;
  const name = [d.last_name.trim(), d.first_name.trim()].filter(Boolean).join(" ") || `slot ${editing.slot}`;
  $("confirm-text").textContent = `Delete ${name}? Their badge stops working immediately. This can't be undone here.`;
  showConfirm(true);
  $("confirm-keep").focus();
}

async function deletePerson() {
  const btn = $("confirm-delete");
  const slot = editing.slot;
  const d = editing.data;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  try {
    await apiWrite("DELETE", `/api/people/${slot}`, { confirm_slot: slot });
    const name = [d.last_name.trim(), d.first_name.trim()].filter(Boolean).join(" ") || `slot ${slot}`;
    flash(`Deleted ${name}.`);
    $("person").close();
    await loadPeople(true);
  } catch (err) {
    showConfirm(false);
    setPersonError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Delete person";
  }
}

// Wait for the next unknown badge swiped on any reader, and use its number.
const SWIPE_TIMEOUT_S = 60;
const SWIPE_POLL_MS = 2500;
let swipe = null;   // { after, deadline, timer }

function setSwipeStatus(msg, found) {
  const s = $("swipe-status");
  s.textContent = msg || "";
  s.hidden = !msg;
  s.classList.toggle("found", !!found);
}

function stopSwipe() {
  if (swipe) clearTimeout(swipe.timer);
  swipe = null;
  $("read-badge").textContent = "Read from a reader";
}

async function swipeTick() {
  if (!swipe) return;
  const left = Math.max(0, Math.round((swipe.deadline - Date.now()) / 1000));
  try {
    const r = await api(`/api/badge-swipe?after=${swipe.after}`);
    if (!swipe) return;              // stopped while waiting
    if (r.badge) {
      const where = r.badge.door ? ` at ${r.badge.door}` : "";
      const time = r.badge.date.split(" ")[1] || r.badge.date;
      stopSwipe();
      if (r.badge.already_known) {
        setSwipeStatus(`Badge ${r.badge.identifier} was read${where}, but it already belongs to someone.`);
      } else {
        $("f-ident").value = r.badge.identifier;
        setSwipeStatus(`Read badge ${r.badge.identifier}${where} at ${time}.`, true);
      }
      return;
    }
  } catch (err) {
    stopSwipe();
    setSwipeStatus(`Couldn't read from the controller: ${err.message}`);
    return;
  }
  if (left <= 0) {
    stopSwipe();
    setSwipeStatus("No new badge was swiped. Press Read from a reader to try again.");
    return;
  }
  setSwipeStatus(`Swipe the new badge on any reader now (${left} s left).`);
  swipe.timer = setTimeout(swipeTick, SWIPE_POLL_MS);
}

async function toggleSwipe() {
  if (swipe) {
    stopSwipe();
    setSwipeStatus("");
    return;
  }
  $("read-badge").textContent = "Stop waiting";
  setSwipeStatus("Getting ready…");
  try {
    const r = await api("/api/badge-swipe?after=0");
    swipe = { after: r.latest_no, deadline: Date.now() + SWIPE_TIMEOUT_S * 1000, timer: null };
    swipeTick();
  } catch (err) {
    stopSwipe();
    setSwipeStatus(`Couldn't read from the controller: ${err.message}`);
  }
}

function setupPersonDialog() {
  $("read-badge").addEventListener("click", toggleSwipe);
  $("person").addEventListener("close", () => { stopSwipe(); });
  $("activity-rows").addEventListener("click", (ev) => {
    const b = ev.target.closest(".row-action");
    if (b) openNewPerson(b.dataset.ident);
  });
  $("people-rows").addEventListener("click", (ev) => {
    const b = ev.target.closest(".person-link");
    if (b) openPerson(Number(b.dataset.slot));
  });
  $("add-person").addEventListener("click", () => openNewPerson());
  $("person-form").addEventListener("submit", savePerson);
  $("person-cancel").addEventListener("click", () => $("person").close());
  $("person-delete").addEventListener("click", askDelete);
  $("confirm-keep").addEventListener("click", () => { showConfirm(false); $("person-delete").focus(); });
  $("confirm-delete").addEventListener("click", deletePerson);
  $("f-valid").addEventListener("change", () => { $("f-dates").hidden = !$("f-valid").checked; });
}


// ---------------------------------------------------------------- shared: compact event lists
function renderMiniEvents(list, events, secondary, emptyText) {
  if (!events.length) {
    list.replaceChildren(el("li", null, el("span", { class: "empty", text: emptyText })));
    return;
  }
  list.replaceChildren(...events.map((e) => {
    const d = parseIso(e.iso);
    const when = d ? (isToday(d) ? `${clock(d)}:${pad(d.getSeconds())}` : `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${clock(d)}`) : e.date;
    const sub = secondary === "door" ? e.door : (e.person || (e.identifier ? `badge ${e.identifier}` : ""));
    return el("li", null,
      el("span", { class: "when", text: when }),
      el("span", { class: "what" }, el("span", { class: `mark ${e.category}`, "aria-hidden": "true" }), e.label,
        sub ? el("span", { class: "sub", text: `, ${sub}` }) : null, e.detail ? el("span", { class: "sub", text: ` (${e.detail})` }) : null));
  }));
}

// Jump to the Activity tab with a search filled in (all event kinds, last 30 days).
function showInActivity(query) {
  for (const id of ["person", "door"]) if ($(id).open) $(id).close();
  document.getElementById("tab-activity").click();
  for (const b of document.querySelectorAll("#panel-activity .seg button")) {
    b.setAttribute("aria-pressed", String(b.dataset.filter === "all"));
  }
  state.activityFilter = "all";
  $("activity-period").value = "30";
  $("activity-search").value = query;
  loadActivity("reset").catch((err) => showError(err.message));
  $("panel-activity").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- door panel
let currentDoor = null;

function renderDoorPanel(d) {
  currentDoor = d;
  $("door-title").textContent = d.name;
  $("door-sub").textContent = `Door ${d.id}`;
  const rows = [
    ["Openings", "openings"], ["  by badge", "badge"], ["  by push button", "push_button"],
    ["  remote", "remote"], ["Refused", "refused"], ["Alarms", "alarms"],
  ];
  $("door-stats").replaceChildren(...rows.map(([label, k]) =>
    el("tr", null, el("td", { text: label.trim() }), el("td", { text: String(d.today[k]) }), el("td", { text: String(d.week[k]) }))));
  const lo = d.last_opening;
  if (lo) {
    const who = lo.person || (lo.identifier ? `badge ${lo.identifier}` : "");
    $("door-last").textContent = `Last opened ${whenText(lo.iso)}: ${lo.label}${who ? `, ${who}` : ""}.`;
  } else {
    $("door-last").textContent = "No opening recorded in the history.";
  }
  renderMiniEvents($("door-events"), d.recent, "person", "Nothing recorded at this door yet.");
  $("door-open").hidden = !d.open_enabled;
  $("door-open-off").hidden = d.open_enabled;
}

async function openDoorPanel(id) {
  try {
    const d = await api(`/api/doors/${id}`);
    $("door-error").hidden = true;
    $("door-result").hidden = true;
    $("door-confirm").hidden = true;
    $("door-actions").hidden = false;
    renderDoorPanel(d);
    $("door").showModal();
    $("door-close").focus();
  } catch (err) {
    showError(err.message);
  }
}

function askOpenDoor() {
  if (!currentDoor) return;
  $("door-confirm-text").textContent = `Open ${currentDoor.name} now? It unlocks for a few seconds, as if its exit button were pressed.`;
  $("door-actions").hidden = true;
  $("door-confirm").hidden = false;
  $("door-keep").focus();
}

async function sendOpenDoor() {
  const d = currentDoor;
  const btn = $("door-confirm-open");
  btn.disabled = true;
  btn.textContent = "Opening…";
  const result = $("door-result");
  try {
    const r = await apiWrite("POST", `/api/doors/${d.id}/open`, { confirm_door: d.id });
    let msg;
    if (r.confirmed) {
      const t = parseIso(r.event.iso);
      msg = `${d.name} opened at ${clock(t)}:${pad(t.getSeconds())}. The controller logged the remote opening.`;
    } else if (r.accepted) {
      msg = `The controller accepted the command but logged no opening within 10 seconds. Check the door. If it stayed locked, remote opening is probably turned off in the controller's settings ("Autoriser l'ouverture à distance").`;
    } else {
      msg = `The controller refused to open the door: ${r.detail}.`;
    }
    result.textContent = msg;
    result.hidden = false;
    result.classList.toggle("warn", !r.confirmed);
    renderDoorPanel(await api(`/api/doors/${d.id}`));
  } catch (err) {
    $("door-error").textContent = err.message;
    $("door-error").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Open door";
    $("door-confirm").hidden = true;
    $("door-actions").hidden = false;
  }
}

function setupDoorPanel() {
  $("doors").addEventListener("click", (ev) => {
    const b = ev.target.closest(".door-link");
    if (b) openDoorPanel(Number(b.dataset.door));
  });
  $("door-close").addEventListener("click", () => $("door").close());
  $("door-open").addEventListener("click", askOpenDoor);
  $("door-keep").addEventListener("click", () => { $("door-confirm").hidden = true; $("door-actions").hidden = false; $("door-open").focus(); });
  $("door-confirm-open").addEventListener("click", sendOpenDoor);
  $("door-all").addEventListener("click", () => currentDoor && showInActivity(currentDoor.name));
}


// ---------------------------------------------------------------- access hours: groups and time slots
const NO_ACCESS = 255, ANY_TIME = 0;
const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday"];
const DAY_LABELS = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday",
                     sat: "Saturday", sun: "Sunday", holiday: "Public holidays" };
const DAY_SHORT = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun", holiday: "Holidays" };
let accessData = null;
let editingGroup = null;   // group object or { id: null } for a new one
let editingSlot = null;

async function loadAccess() {
  accessData = await api("/api/access");
  renderAccess();
}

function slotName(id) {
  const s = accessData && accessData.time_slots.find((x) => x.id === id);
  return s ? s.name : `Time slot ${id}`;
}

function accessText(v) {
  if (v === NO_ACCESS) return "No access";
  if (v === ANY_TIME) return "Any time";
  return slotName(v);
}

const minutes = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

function dayBar(ranges, holiday) {
  const bar = el("div", { class: holiday ? "daybar holiday" : "daybar", "aria-hidden": "true" });
  for (const r of ranges) {
    const [a, b] = r.split("-");
    const span = el("span");
    span.style.left = `${(minutes(a) / 1440) * 100}%`;
    span.style.width = `${((minutes(b) - minutes(a)) / 1440) * 100}%`;
    bar.append(span);
  }
  return bar;
}

// "Mon–Fri 08:00–18:00; Sat 09:00–12:00" (holidays listed separately)
function summariseWeek(days) {
  const week = DAY_KEYS.slice(0, 7);
  const text = (k) => days[k].map((r) => r.replace("-", "–")).join(", ");
  const parts = [];
  let i = 0;
  while (i < week.length) {
    let j = i;
    while (j + 1 < week.length && text(week[j + 1]) === text(week[i])) j++;
    if (days[week[i]].length) {
      const span = i === j ? DAY_SHORT[week[i]] : `${DAY_SHORT[week[i]]}–${DAY_SHORT[week[j]]}`;
      parts.push(`${span} ${text(week[i])}`);
    }
    i = j + 1;
  }
  let out = parts.length ? parts.join("; ") : "No hours set: no access on any day";
  out += days.holiday.length ? `. Holidays ${text("holiday")}.` : ". No access on public holidays.";
  return out;
}

function renderAccess() {
  const a = accessData;
  const writable = a.writes_enabled;
  $("add-group").hidden = !writable;
  $("add-slot").hidden = !writable;

  const head = $("group-head");
  head.replaceChildren(el("th", { scope: "col", text: "Group" }),
    ...a.doors.map((d) => el("th", { scope: "col", text: d.name })), el("th", { scope: "col", text: "People" }));
  const cols = a.doors.length + 2;
  if (!a.groups.length) {
    $("group-rows").replaceChildren(el("tr", null, el("td", { class: "none", colspan: String(cols), text: "No groups in use yet." })));
  } else {
    $("group-rows").replaceChildren(...a.groups.map((g) => el("tr", { class: g.enabled ? null : "off" },
      el("td", null, el("button", { type: "button", class: "person-link", "data-group": String(g.id), text: g.name }),
        g.enabled ? null : el("span", { class: "off-tag", text: "Off" })),
      ...a.doors.map((d) => {
        const v = g.doors[String(d.id)];
        return el("td", { class: v === ANY_TIME ? "access-any" : v === NO_ACCESS ? "access-none" : null, text: accessText(v) });
      }),
      el("td", { text: g.members ? `${g.members} (${g.authorised_members} authorised)` : "0" }))));
  }

  const list = $("slot-list");
  if (!a.time_slots.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No time slots yet. Add one to give a group limited hours." }));
  } else {
    list.replaceChildren(...a.time_slots.map((t) => {
      const users = t.used_by.map((id) => { const g = a.groups.find((x) => x.id === id); return g ? g.name : `group ${id}`; });
      const week = el("div", { class: "week" });
      for (const k of DAY_KEYS) week.append(el("span", { text: DAY_SHORT[k] }), dayBar(t.days[k], k === "holiday"));
      return el("li", { class: "slot-item" },
        el("div", { class: "slot-top" },
          el("button", { type: "button", class: "person-link", "data-slot-id": String(t.id), text: t.name }),
          el("span", { class: "help", text: users.length ? `Used by ${users.join(", ")}` : "Not used by any group" })),
        el("p", { class: "slot-summary", text: summariseWeek(t.days) }),
        week);
    }));
  }
  $("holiday-note").textContent = a.holidays
    ? `${a.holidays} public holiday date${a.holidays === 1 ? " is" : "s are"} set on the controller; on those dates the holiday hours apply.`
    : "No public holiday dates are set on the controller, so the holiday hours are never used.";
}

// ---- group dialog
function openGroup(id) {
  const a = accessData;
  const g = id == null ? null : a.groups.find((x) => x.id === id);
  editingGroup = g || { id: null };
  const writable = a.writes_enabled;
  $("group-title").textContent = g ? g.name : "Add a group";
  $("group-sub").textContent = g ? `Group ${g.id}` : "An unused group on the controller is set up for you.";
  $("g-name").value = g ? g.name : "";
  $("g-enabled").checked = g ? g.enabled : true;
  const options = [{ id: NO_ACCESS, name: "No access" }, { id: ANY_TIME, name: "Any time" },
    ...a.time_slots.map((t) => ({ id: t.id, name: t.name }))];
  $("g-doors").replaceChildren(...a.doors.flatMap((d) => {
    const sel = el("select", { id: `g-door-${d.id}` });
    const current = g ? g.doors[String(d.id)] : NO_ACCESS;
    fillSelect(sel, options, current, (n) => `Time slot ${n}`);
    sel.disabled = !writable;
    return [el("label", { for: `g-door-${d.id}`, text: d.name }), sel];
  }));
  $("g-other").hidden = !(g && g.has_other_settings);
  $("g-name").disabled = !writable;
  $("g-enabled").disabled = !writable;
  $("g-save").hidden = !writable;
  $("g-save").textContent = g ? "Save changes" : "Add group";
  $("g-cancel").textContent = writable ? "Cancel" : "Close";
  $("g-members").hidden = !(g && g.members);
  $("g-members").textContent = g ? `Show the ${g.members} ${g.members === 1 ? "person" : "people"} in this group` : "";
  $("group-error").hidden = true;
  $("group").showModal();
  $("g-name").focus();
}

async function saveGroup(ev) {
  ev.preventDefault();
  const g = editingGroup;
  const name = $("g-name").value.trim();
  if (!name) { $("group-error").textContent = "Give the group a name."; $("group-error").hidden = false; return; }
  const body = { name, enabled: $("g-enabled").checked,
                 doors: Object.fromEntries(accessData.doors.map((d) => [String(d.id), Number($(`g-door-${d.id}`).value)])) };
  const btn = $("g-save");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const r = await apiWrite("POST", g.id == null ? "/api/groups" : `/api/groups/${g.id}`, body);
    $("group").close();
    flashAccess(g.id == null ? `Added group ${r.name}.` : `Saved ${r.name}.`);
    await afterAccessChange();
  } catch (err) {
    $("group-error").textContent = err.message;
    $("group-error").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = g.id == null ? "Add group" : "Save changes";
  }
}

// ---- time-slot dialog
function parseRanges(text) {
  return text.split(/[,;]/).map((r) => r.trim().replace(/\s+/g, "").replace("–", "-")).filter(Boolean);
}
const RANGE_RE = /^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$/;

function dayRow(key, ranges, writable) {
  const input = el("input", { id: `s-day-${key}`, autocomplete: "off", spellcheck: "false", placeholder: "No access" });
  input.value = ranges.join(", ");
  input.disabled = !writable;
  const row = el("div", { class: "day-row" }, el("label", { for: input.id, text: DAY_LABELS[key] }), input, dayBar(ranges, key === "holiday"));
  input.addEventListener("input", () => {
    const rs = parseRanges(input.value);
    const valid = rs.every((r) => RANGE_RE.test(r) && minutes(r.slice(6)) > minutes(r.slice(0, 5)));
    row.classList.toggle("invalid", !valid);
    row.replaceChild(dayBar(valid ? rs : [], key === "holiday"), row.lastChild);
  });
  return row;
}

function openSlot(id) {
  const a = accessData;
  const t = id == null ? null : a.time_slots.find((x) => x.id === id);
  editingSlot = t || { id: null };
  const writable = a.writes_enabled;
  $("slot-title").textContent = t ? t.name : "Add a time slot";
  $("slot-sub").textContent = t ? `Time slot ${t.id}` : "An unused time slot on the controller is set up for you.";
  $("s-name").value = t ? t.name : "";
  $("s-name").disabled = !writable;
  const empty = Object.fromEntries(DAY_KEYS.map((k) => [k, []]));
  $("s-days").replaceChildren(...DAY_KEYS.map((k) => dayRow(k, (t ? t.days : empty)[k], writable)));
  for (const b of ["s-copy-week", "s-copy-all"]) $(b).hidden = !writable;
  const users = t ? t.used_by.map((gid) => { const g = a.groups.find((x) => x.id === gid); return g ? g.name : `group ${gid}`; }) : [];
  $("s-used").textContent = users.length ? `Used by ${users.join(", ")}. Changes apply to them immediately.` : "";
  $("s-save").hidden = !writable;
  $("s-save").textContent = t ? "Save changes" : "Add time slot";
  $("s-cancel").textContent = writable ? "Cancel" : "Close";
  $("slot-error").hidden = true;
  $("slot").showModal();
  $("s-name").focus();
}

function copyMonday(targets) {
  const v = $("s-day-mon").value;
  for (const k of targets) {
    const input = $(`s-day-${k}`);
    input.value = v;
    input.dispatchEvent(new Event("input"));
  }
}

async function saveSlot(ev) {
  ev.preventDefault();
  const t = editingSlot;
  const name = $("s-name").value.trim();
  if (!name) { $("slot-error").textContent = "Give the time slot a name."; $("slot-error").hidden = false; return; }
  const days = Object.fromEntries(DAY_KEYS.map((k) => [k, parseRanges($(`s-day-${k}`).value)]));
  const btn = $("s-save");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const r = await apiWrite("POST", t.id == null ? "/api/time-slots" : `/api/time-slots/${t.id}`, { name, days });
    $("slot").close();
    flashAccess(t.id == null ? `Added time slot ${r.name}.` : `Saved ${r.name}.`);
    await afterAccessChange();
  } catch (err) {
    $("slot-error").textContent = err.message;
    $("slot-error").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = t.id == null ? "Add time slot" : "Save changes";
  }
}

function flashAccess(msg) {
  let f = $("access-flash");
  if (!f) {
    f = el("p", { class: "flash", id: "access-flash", role: "status", "aria-live": "polite" });
    $("panel-access").prepend(f);
  }
  f.textContent = msg;
}

async function afterAccessChange() {
  await loadAccess();
  state.options = null;                       // group names in the person form may have changed
  await loadOptions().catch(() => {});
  await loadPeople(true).catch(() => {});
}

function showGroupMembers() {
  const g = editingGroup;
  $("group").close();
  document.getElementById("tab-people").click();
  $("people-search").value = g.name;
  renderPeople();
}

function setupAccess() {
  $("group-rows").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-group]");
    if (b) openGroup(Number(b.dataset.group));
  });
  $("slot-list").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-slot-id]");
    if (b) openSlot(Number(b.dataset.slotId));
  });
  $("add-group").addEventListener("click", () => openGroup(null));
  $("add-slot").addEventListener("click", () => openSlot(null));
  $("group-form").addEventListener("submit", saveGroup);
  $("slot-form").addEventListener("submit", saveSlot);
  $("g-cancel").addEventListener("click", () => $("group").close());
  $("s-cancel").addEventListener("click", () => $("slot").close());
  $("g-members").addEventListener("click", showGroupMembers);
  $("s-copy-week").addEventListener("click", () => copyMonday(["tue", "wed", "thu", "fri"]));
  $("s-copy-all").addEventListener("click", () => copyMonday(["tue", "wed", "thu", "fri", "sat", "sun", "holiday"]));
}

// ---------------------------------------------------------------- wiring
function setupSegments(container, key, render) {
  container.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    for (const other of container.querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === b));
    state[key] = b.dataset.filter || b.dataset.status;
    render();
  });
}

function setupTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (tab.id === "tab-people") loadPeople(false).catch((err) => showError(err.message));
    if (tab.id === "tab-access") loadAccess().catch((err) => showError(err.message));
  };
  for (const t of tabs) {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
      const i = tabs.indexOf(t) + (ev.key === "ArrowRight" ? 1 : -1);
      const next = tabs[(i + tabs.length) % tabs.length];
      next.focus();
      select(next);
    });
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  renderHours();
  setupTooltip();
  setupTabs();
  const reloadActivity = () => loadActivity("reset").catch((err) => showError(err.message));
  setupSegments(document.querySelector("#panel-activity .seg"), "activityFilter", reloadActivity);
  setupSegments(document.querySelector("#panel-people .seg"), "peopleFilter", renderPeople);
  let searchTimer = null;
  $("activity-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(reloadActivity, SEARCH_DELAY_MS);
  });
  $("activity-period").addEventListener("change", reloadActivity);
  $("people-sort").addEventListener("change", renderPeople);
  $("people-search").addEventListener("input", renderPeople);
  $("load-older").addEventListener("click", loadOlder);
  $("refresh").addEventListener("click", () => { refresh(true); refreshOverview(); });
  setupPersonDialog();
  setupDoorPanel();
  setupAccess();
  loadOptions().catch(() => {});

  await refreshOverview();
  await loadActivity("reset").catch((err) => showError(err.message));
  await refresh(false);
  loadPeople(false).catch(() => {});  // fills the tab count in the background

  setInterval(() => { if (!document.hidden) refresh(false); }, REFRESH_MS);
  setInterval(() => { if (!document.hidden) refreshOverview(); }, OVERVIEW_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
});
