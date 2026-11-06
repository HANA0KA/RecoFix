"use strict";
/* RecoFix web UI. Vanilla JS, no dependencies; talks to the local engine at /api. */

const TOKEN = new URLSearchParams(location.search).get("t") || sessionStorage.getItem("rf_t") || "";
sessionStorage.setItem("rf_t", TOKEN);
const S = { env: {}, view: "home", scan: null, timers: [] };

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, body, opts = {}) {
  const r = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (opts.raw) return r;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

const fmtB = (n) => {
  if (n == null || n < 0) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : n < 100 ? 1 : 0) : n) + " " + u[i];
};
const fmtT = (s) => {
  if (s == null || !isFinite(s)) return "—";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh} h ${mm} min` : mm ? `${mm} min ${ss} s` : `${ss} s`;
};
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" }) : "—";
const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");

function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + kind }, msg);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}

function every(ms, fn) {
  const id = setInterval(fn, ms);
  S.timers.push(id);
  fn();
  return id;
}
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }

const ICONS = {
  disk: '<path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M4 14h16"/><circle cx="16.5" cy="17" r=".8"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  sd: '<path d="M7 3h8l4 4v14H7z"/><path d="M10 3v4M13 3v4M16 4v3"/>',
  part: '<circle cx="12" cy="12" r="8"/><path d="M12 4v8l6 5"/>',
  video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
  tm: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/>',
  nas: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  crash: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4M9 8l6 4M15 8l-6 4"/>',
  cloud: '<path d="M7 18a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10a4 4 0 0 1-1 8z"/>',
  rvideo: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3M7 12l2 2 3-4"/>',
  rphoto: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
  rdoc: '<path d="M6 3h8l5 5v13H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  smart: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  history: '<path d="M4 5h16M4 12h16M4 19h10"/>',
  mhl: '<path d="m5 12 4 4 10-10"/><path d="M4 20h16"/>',
  log: '<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>',
  home: '<path d="m3 11 9-7 9 7M5 10v10h14V10"/>',
  folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
  file: '<path d="M6 3h8l5 5v13H6z"/><path d="M14 3v5h5"/>',
};
const ico = (k) => h("span", { html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[k] || ICONS.file}</svg>`, style: { display: "contents" } });

const CAT = { video: "Vídeo", photo: "Foto", audio: "Audio", document: "Documento", archive: "Comprimido", other: "Otros", system: "Sistema" };
const GROUP = { existing: "Existentes", deleted: "Borrados", orphan: "Huérfanos (tras formateo)", raw: "Sin nombre (RAW)",
  zeroed: "Rescatados (MHL)" };

// ---------------------------------------------------------------- navigation

const NAV = [
  ["Recuperar", [
    ["home", "Inicio", "home"],
    ["disk", "Discos y ubicaciones", "disk"],
    ["trash", "Papelera", "trash"],
    ["sd", "Tarjeta SD y cámaras", "sd"],
    ["video", "Vídeo avanzado", "video"],
    ["zeroed", "Rescate de clips vaciados", "rvideo"],
    ["partition", "Particiones perdidas", "part"],
    ["tm", "Time Machine", "tm"],
    ["icloud", "iCloud Drive", "cloud"],
    ["nas", "NAS y Linux", "nas"],
    ["imagecfg", "Imagen de disco", "image"],
    ["crash", "Mac que no arranca", "crash"],
  ]],
  ["Reparar", [
    ["rvideo", "Reparar vídeo", "rvideo"],
    ["rphoto", "Reparar fotos", "rphoto"],
    ["rdoc", "Reparar documentos", "rdoc"],
  ]],
  ["Herramientas", [
    ["smart", "Salud del disco", "smart"],
    ["history", "Historial de escaneos", "history"],
    ["mhl", "Verificar MHL", "mhl"],
    ["log", "Registro", "log"],
  ]],
];
const TITLES = Object.fromEntries(NAV.flatMap(([, it]) => it.map(([k, t]) => [k, t])));

function renderNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...NAV.map(([sec, items]) => [h("h6", {}, sec), ...items.map(([k, t, i]) =>
    h("a", { class: S.view === k || (S.view === "results" && S.scanFrom === k) ? "on" : "", onclick: () => go(k) }, ico(i), t))]).flat());
}

function go(view, arg) {
  clearTimers();
  S.view = view;
  renderNav();
  const v = document.getElementById("view");
  v.replaceChildren();
  v.scrollTop = 0;
  document.getElementById("crumbs").replaceChildren(TITLES[view] || (view === "results" ? "Resultados" : ""));
  (VIEWS[view] || VIEWS.home)(v, arg);
}

function page(title, lead, ...body) {
  return h("div", { class: "page" }, h("h1", {}, title), lead ? h("p", { class: "lead" }, lead) : null, ...body);
}

// ---------------------------------------------------------------- modal + file browser

function modal(title, body, buttons) {
  const m = document.getElementById("modal");
  const close = () => m.classList.add("hidden");
  const foot = h("footer", {}, ...(buttons || []).map((b) => b));
  m.replaceChildren(h("div", { class: "dlg" }, h("header", {}, title, h("span", { class: "spacer" }),
    h("button", { class: "ghost small", onclick: close }, "✕")), h("div", { class: "body" }, body), foot));
  m.classList.remove("hidden");
  m.onclick = (e) => { if (e.target === m) close(); };
  return close;
}

/** Pick a folder (mode "dir") or file(s) (mode "file"/"files"). Resolves with path or [paths] or null. */
function pickPath({ mode = "dir", title, start, show = "all", allowNew = false } = {}) {
  return new Promise((resolve) => {
    let cur = start || "/Volumes", sel = new Set();
    const list = h("div", { class: "fb" });
    const where = h("input", { type: "text", class: "grow mono", value: cur });
    const info = h("span", { class: "muted" });
    const load = async (p) => {
      try {
        const d = await api("ls?" + qs({ path: p, show }));
        cur = d.path; where.value = cur; sel.clear();
        info.textContent = "Libre: " + fmtB(d.free);
        const up = h("div", { class: "e", onclick: () => load(d.parent) }, ico("folder"), "..");
        list.replaceChildren(up, ...d.entries.map((e) => {
          const row = h("div", { class: "e" }, ico(e.dir ? "folder" : "file"), h("span", { class: "grow" }, e.name),
            e.dir ? null : h("span", { class: "muted" }, fmtB(e.size)));
          row.onclick = () => {
            if (e.dir && mode === "dir") { load(e.path); return; }
            if (e.dir) { load(e.path); return; }
            if (mode === "files") { sel.has(e.path) ? sel.delete(e.path) : sel.add(e.path); row.classList.toggle("sel"); }
            else { list.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel")); sel = new Set([e.path]); row.classList.add("sel"); }
          };
          row.ondblclick = () => { if (!e.dir && mode !== "dir") { done([e.path]); } };
          return row;
        }));
      } catch (err) { toast(err.message, "bad"); }
    };
    const done = (val) => { close(); resolve(mode === "dir" ? val : mode === "file" ? (val[0] || null) : val); };
    const quick = h("div", { class: "row" },
      ...[["Volúmenes", "/Volumes"], ["Inicio", S.env.home], ["Escritorio", S.env.home + "/Desktop"], ["Películas", S.env.home + "/Movies"]]
        .map(([t, p]) => h("button", { class: "small", onclick: () => load(p) }, t)),
      allowNew ? h("button", { class: "small", onclick: async () => {
        const n = prompt("Nombre de la nueva carpeta", "RecoFix_Recuperado");
        if (n) { await api("mkdir", { path: cur + "/" + n }); load(cur + "/" + n); }
      } }, "Nueva carpeta") : null);
    where.onkeydown = (e) => { if (e.key === "Enter") load(where.value); };
    const ok = h("button", { class: "primary", onclick: () => done(mode === "dir" ? cur : [...sel]) },
      mode === "dir" ? "Elegir esta carpeta" : "Elegir");
    const close = modal(title || (mode === "dir" ? "Elegir carpeta" : "Elegir archivo"),
      h("div", { class: "col" }, quick, h("div", { class: "row" }, where), list),
      [info, h("span", { class: "spacer" }), h("button", { onclick: () => { close(); resolve(null); } }, "Cancelar"), ok]);
    load(cur);
  });
}

// ---------------------------------------------------------------- jobs drawer

async function refreshJobs() {
  try {
    const jobs = await api("jobs");
    const live = jobs.filter((j) => !j.done);
    document.getElementById("jobscount").textContent = live.length;
    const d = document.getElementById("jobs");
    if (d.classList.contains("hidden")) return;
    d.replaceChildren(h("div", { class: "row" }, h("b", {}, "Tareas"), h("span", { class: "spacer" }),
      h("button", { class: "ghost small", onclick: () => d.classList.add("hidden") }, "✕")),
      ...(jobs.length ? jobs.slice().reverse().map((j) => h("div", { class: "job" },
        h("div", { class: "row" }, h("b", { class: "grow" }, j.label || j.kind), h("span", { class: "tag " + (j.error ? "bad" : j.done ? "ok" : "acc") }, j.error ? "error" : j.phase)),
        h("div", { class: "bar", style: { marginTop: "8px" } }, h("i", { style: { width: (100 * (j.progress || 0)).toFixed(1) + "%" } })),
        j.kind === "scan" ? h("button", { class: "small", style: { marginTop: "8px" }, onclick: () => { d.classList.add("hidden"); openScan(j.id); } }, "Ver resultados") : null,
        j.error ? h("div", { class: "muted", style: { marginTop: "6px" } }, j.error) : null))
        : [h("p", { class: "muted" }, "No hay tareas.")]));
  } catch (e) { /* engine restarting */ }
}

/** Poll a generic job; resolves with final status. */
function waitJob(id, onTick) {
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const st = await api("job/" + id);
        onTick && onTick(st);
        if (st.done) { clearInterval(t); resolve(st); }
      } catch (e) { clearInterval(t); resolve({ error: e.message, done: true }); }
    };
    const t = setInterval(tick, 700);
    S.timers.push(t);
    tick();
  });
}

// ---------------------------------------------------------------- views

const VIEWS = {};

VIEWS.home = (v) => {
  const modes = [
    ["disk", "disk", "Discos y ubicaciones", "Disco interno, externo, partición, Escritorio o una carpeta concreta."],
    ["trash", "trash", "Papelera", "Archivos en la Papelera, con su ubicación original."],
    ["sd", "sd", "Tarjeta SD y cámaras", "Perfil automático por marca: Sony, Canon, ARRI, RED, DJI, GoPro…"],
    ["video", "video", "Vídeo avanzado", "Reensambla clips MP4/MOV fragmentados frame a frame."],
    ["zeroed", "rvideo", "Rescate de clips vaciados", "Clips que figuran con 0 bytes pero siguen en el disco: se localizan con el MHL, se reconstruyen y se verifican con su hash."],
    ["partition", "part", "Particiones perdidas", "Encuentra volúmenes borrados o tablas de particiones dañadas."],
    ["tm", "tm", "Time Machine", "Copias y instantáneas locales: busca y restaura intacto."],
    ["nas", "nas", "NAS y Linux", "Conecta por SSH y recupera de discos remotos sin escribir en ellos."],
    ["imagecfg", "image", "Imagen de disco", "Clona un disco con sectores dañados y escanea la copia."],
    ["crash", "crash", "Mac que no arranca", "Kit USB para macOS Recovery y modo Compartir disco."],
    ["rvideo", "rvideo", "Reparar vídeo", "Rápida o avanzada con un vídeo de muestra de la misma cámara."],
    ["rphoto", "rphoto", "Reparar fotos", "JPEG dañados, PNG truncados y vistas previas de RAW."],
    ["rdoc", "rdoc", "Reparar documentos", "Office, iWork, ODF, ZIP, PDF y SQLite."],
  ];
  const devs = h("div", { class: "grid" }, h("div", { class: "card" }, h("span", { class: "spin" }), " Buscando discos…"));
  v.append(page("¿Qué necesitas recuperar?", "Elige un modo. RecoFix solo lee del origen y nunca escribe en el disco que recupera.",
    h("div", { class: "mode-grid" }, ...modes.map(([k, i, t, d]) =>
      h("div", { class: "card click", onclick: () => go(k) }, h("div", { class: "ico" }, ico(i)), h("h3", {}, t), h("p", {}, d)))),
    h("div", { class: "section-title" }, h("h2", {}, "Discos conectados"), h("span", { class: "muted" }, "clic para escanear")), devs));
  api("devices").then((ds) => {
    devs.replaceChildren(...ds.map((d) => h("div", { class: "card click", onclick: () => go("disk", { select: d.path }) },
      h("div", { class: "dev" }, h("div", { class: "disk-ico" }, ico(d.is_image ? "image" : d.removable ? "sd" : "disk")),
        h("div", {}, h("b", {}, d.name), h("div", { class: "muted" }, fmtB(d.size), " · ", d.id, d.protocol ? " · " + d.protocol : "")),
        d.profile ? h("span", { class: "tag acc" }, d.profile.brand) : null))));
  }).catch((e) => devs.replaceChildren(h("div", { class: "notice bad" }, e.message)));
};

// ------------------------------------------------ disk & locations (also used by sd / video / partition)

function scanOptions(defaults) {
  const o = Object.assign({ deep: true, categories: [], lost: true, exts: "" }, defaults);
  const cats = ["video", "photo", "audio", "document", "archive", "other"];
  const seg = h("div", { class: "seg" },
    h("button", { class: o.deep ? "" : "on", onclick: (e) => { o.deep = false; seg.children[0].className = "on"; seg.children[1].className = ""; } }, "Rápido"),
    h("button", { class: o.deep ? "on" : "", onclick: (e) => { o.deep = true; seg.children[1].className = "on"; seg.children[0].className = ""; } }, "Profundo"));
  const chips = h("div", { class: "chips" }, ...cats.map((c) => {
    const ch = h("span", { class: "chip" + (o.categories.includes(c) ? " on" : "") }, CAT[c]);
    ch.onclick = () => { ch.classList.toggle("on"); o.categories = o.categories.includes(c) ? o.categories.filter((x) => x !== c) : [...o.categories, c]; };
    return ch;
  }));
  const lost = h("input", { type: "checkbox", checked: o.lost ? true : null, onchange: (e) => { o.lost = e.target.checked; } });
  const exts = h("input", { type: "text", placeholder: "extensiones (mxf, mov…)", value: o.exts, style: { width: "170px" }, oninput: (e) => { o.exts = e.target.value; } });
  const el = h("div", { class: "row", style: { gap: "16px" } },
    h("div", { class: "col", style: { gap: "4px" } }, h("small", { class: "muted" }, "Modo"), seg),
    h("div", { class: "col", style: { gap: "4px" } }, h("small", { class: "muted" }, "Tipos (vacío = todos)"), chips),
    h("div", { class: "col", style: { gap: "4px" } }, h("small", { class: "muted" }, "Solo estas extensiones"), exts),
    h("label", { class: "chk" }, lost, "Buscar particiones perdidas"));
  return { el, get: () => ({ deep: o.deep, categories: o.categories, lost_partitions: o.lost,
    exts: o.exts.split(/[ ,]+/).map((x) => x.trim().replace(/^\./, "")).filter(Boolean) }) };
}

async function startScan(body, from) {
  try {
    const r = await api("scan", body);
    S.scanFrom = from;
    toast("Escaneo iniciado");
    openScan(r.id);
  } catch (e) { toast(e.message, "bad"); }
}

function diskPicker(container, { onSelect, filter, preselect } = {}) {
  let selected = null;
  const render = (ds) => {
    container.replaceChildren(...ds.filter(filter || (() => true)).map((d) => {
      const whole = h("div", { class: "part", "data-path": d.path }, h("b", { class: "grow" }, "Disco completo"),
        h("span", { class: "muted" }, fmtB(d.size)));
      const parts = d.partitions.map((p) => h("div", { class: "part", "data-path": p.path },
        h("span", { class: "grow" }, p.name || p.id, " ", h("span", { class: "faint" }, p.content)),
        p.mount ? h("span", { class: "tag" }, p.mount) : null, h("span", { class: "muted" }, fmtB(p.size))));
      const card = h("div", { class: "card" }, h("div", { class: "dev" }, h("div", { class: "disk-ico" }, ico(d.is_image ? "image" : d.removable ? "sd" : "disk")),
        h("div", {}, h("b", {}, d.name), h("div", { class: "muted" }, d.id, " · ", fmtB(d.size), d.internal ? " · interno" : " · externo", d.protocol ? " · " + d.protocol : "")),
        d.profile ? h("span", { class: "tag acc" }, d.profile.brand + " · " + d.profile.line) : null),
        h("div", { class: "parts" }, whole, ...parts));
      [whole, ...parts].forEach((el) => el.onclick = () => {
        container.querySelectorAll(".part.sel").forEach((x) => x.classList.remove("sel"));
        el.classList.add("sel");
        selected = { path: el.dataset.path, disk: d, label: el === whole ? d.name : (el.querySelector("span").textContent || d.name) };
        onSelect && onSelect(selected);
      });
      if (preselect && (preselect === d.path)) setTimeout(() => whole.click());
      return card;
    }));
    if (!container.children.length) container.append(h("div", { class: "empty" }, "No hay discos que coincidan."));
  };
  container.replaceChildren(h("div", { class: "card" }, h("span", { class: "spin" }), " Leyendo discos…"));
  api("devices").then(render).catch((e) => container.replaceChildren(h("div", { class: "notice bad" }, e.message)));
  return () => selected;
}

VIEWS.disk = (v, arg) => {
  let target = null;
  const opts = scanOptions({});
  const startBtn = h("button", { class: "primary", disabled: true, onclick: () => {
    const o = opts.get();
    startScan({ path: target.path, label: target.label, mode: "disk", scope: target.scope, deep: o.deep,
      categories: o.categories, exts: o.exts, lost_partitions: o.lost_partitions }, "disk");
  } }, "Escanear");
  const chosen = h("span", { class: "muted" }, "Elige un disco, partición o ubicación");
  const select = (t) => { target = t; startBtn.disabled = false; chosen.replaceChildren(h("b", {}, t.label), t.scope ? " · carpeta " + t.scope : ""); };
  const disks = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  diskPicker(disks, { onSelect: (s) => select(s), preselect: arg && arg.select });
  const locs = h("div", { class: "row" });
  api("locations").then((ls) => locs.replaceChildren(...ls.filter((l) => l.key !== "trash").map((l) =>
    h("button", { disabled: !l.exists || null, onclick: () => chooseFolder(l.path, l.label) }, l.label)),
    h("button", { onclick: async () => { const p = await pickPath({ mode: "dir", start: S.env.home }); if (p) chooseFolder(p); } }, "Elegir carpeta…"),
    h("button", { onclick: () => go("trash") }, "Papelera"),
    h("button", { onclick: async () => {
      const p = await pickPath({ mode: "file", show: "images", title: "Abrir imagen de disco" });
      if (p) select({ path: p, label: p.split("/").pop() });
    } }, "Abrir imagen de disco…")));
  const warn = h("div");
  async function chooseFolder(path, label) {
    try {
      const r = await api("locations/resolve", { path });
      warn.replaceChildren(r.warning ? h("div", { class: "notice warn", style: { marginTop: "10px" } }, r.warning) : null);
      select({ path: r.device, label: label || path.split("/").pop(), scope: r.metadata_scan ? r.scope : null });
    } catch (e) { toast(e.message, "bad"); }
  }
  v.append(page("Discos y ubicaciones", "Escaneo rápido: lee el sistema de archivos (archivos borrados con su nombre y carpeta). " +
    "Profundo: además recorre cada sector buscando archivos por su estructura interna, incluso tras formatear.",
    h("h2", {}, "Ubicaciones"), locs, warn, h("h2", {}, "Discos"), disks,
    h("div", { class: "opts" }, opts.el, h("span", { class: "spacer" }), chosen, startBtn)));
};

VIEWS.sd = (v) => {
  const list = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  const detail = h("div");
  let target = null;
  const startBtn = h("button", { class: "primary", disabled: true }, "Escanear tarjeta");
  startBtn.onclick = () => {
    const p = target.disk.profile;
    startScan({ path: target.path, label: (p ? p.brand + " · " : "") + target.label, mode: "sd", deep: true,
      categories: p ? p.categories : ["video", "photo"], lost_partitions: true }, "sd");
  };
  diskPicker(list, { filter: (d) => !d.internal && !d.is_image, onSelect: (s) => {
    target = s; startBtn.disabled = false;
    const p = s.disk.profile;
    detail.replaceChildren(p ? h("div", { class: "notice ok" }, `Perfil ${p.brand} (${p.line}) listo`, p.camera ? ` · ${p.make} ${p.camera}` : "",
      ` · formatos prioritarios: ${p.formats.join(", ")}`) : h("div", { class: "notice" }, "No se reconoció la estructura de cámara (¿formateada?). Se buscarán vídeo y fotos de todas las marcas."));
  } });
  v.append(page("Tarjeta SD y cámaras", "Detecta DCIM, PRIVATE/M4ROOT, CONTENTS, XDROOT y las estructuras de ARRI, RED o Blackmagic para escanear con el perfil de la marca. Funciona también con tarjetas formateadas.",
    list, h("div", { style: { marginTop: "14px" } }, detail),
    h("div", { class: "opts" }, h("span", { class: "muted" }, "Consejo: si la tarjeta da errores de lectura, crea antes una imagen de disco."), h("span", { class: "spacer" }), startBtn)));
};

VIEWS.video = (v) => {
  let target = null;
  const list = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  const startBtn = h("button", { class: "primary", disabled: true, onclick: () =>
    startScan({ path: target.path, label: "Vídeo · " + target.label, mode: "video", deep: true, categories: ["video"], lost_partitions: true }, "video") }, "Buscar vídeos");
  diskPicker(list, { onSelect: (s) => { target = s; startBtn.disabled = false; } });
  v.append(page("Recuperación avanzada de vídeo", "Escaneo profundo centrado en vídeo. Los clips MP4/MOV que aparezcan incompletos (índice moov fuera de lugar por fragmentación) " +
    "se pueden reensamblar: RecoFix usa la tabla de muestras del clip para localizar cada fotograma en el disco.",
    h("ol", { class: "steps", style: { margin: "0 0 18px" } }, h("li", {}, "Elige el disco o la tarjeta."), h("li", {}, "Al terminar, filtra Vídeo y busca la etiqueta «incompleto»."),
      h("li", {}, "Selecciona el clip y pulsa «Reensamblar vídeo» en el panel de detalle.")),
    list, h("div", { class: "opts" }, h("span", { class: "spacer" }), startBtn)));
};

VIEWS.partition = (v) => {
  let target = null;
  const list = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  const deep = h("input", { type: "checkbox" });
  const startBtn = h("button", { class: "primary", disabled: true, onclick: () =>
    startScan({ path: target.disk.path, label: "Particiones · " + target.disk.name, mode: "partition", deep: deep.checked, lost_partitions: true }, "partition") }, "Buscar particiones");
  diskPicker(list, { onSelect: (s) => { target = s; startBtn.disabled = false; } });
  v.append(page("Particiones perdidas", "Busca en el espacio sin particionar los sectores de arranque y superbloques de exFAT, FAT, NTFS, APFS, HFS+ y ext; " +
    "los volúmenes encontrados se abren como cualquier otro, con nombres y carpetas.",
    list, h("div", { class: "opts" }, h("label", { class: "chk" }, deep, "Añadir escaneo profundo"), h("span", { class: "spacer" }), startBtn)));
};

// ------------------------------------------------ rescue of emptied clips

const ZNAME = "Rescate de clips vaciados";
const ZSTAGES = [["leer", "Leer volumen"], ["mhl", "Leer MHL"], ["buscar", "Buscar cabeceras"], ["emparejar", "Emparejar y verificar"], ["fin", "Terminado"]];

function zerosPlural(n, one, many) { return n + " " + (n === 1 ? one : many); }

// Offer the remedy attached to an unresolved clip: add a MHL, or widen the search.
function zeroedAction(u) {
  const body = S.zeroedBody;
  if (!body || !u.action) return null;
  if (u.action === "add_mhl") {
    const add = (mode, title, label) => h("button", { class: "small", onclick: async () => {
      const p = await pickPath({ mode, show: "manifest", title, start: S.env.home });
      if (p) startScan(Object.assign({}, body, { mhl: (body.mhl || []).concat([p]) }), "zeroed");
    } }, label);
    return h("div", { class: "row", style: { gap: "6px" } }, add("file", "Manifiesto de hashes (.mhl, .md5, .sha1, .sha256…)", "Añadir manifiesto y repetir"),
      add("dir", "Carpeta con manifiestos de hashes", "Añadir carpeta de manifiestos y repetir"));
  }
  if (u.action === "search_all" && body.search !== "all") {
    return h("button", { class: "small", onclick: () => startScan(Object.assign({}, body, { search: "all", range: null }), "zeroed") }, "Buscar en todo el volumen");
  }
  return null;
}

function zeroedCard(u) {
  return h("div", { class: "zcard", title: u.path },
    h("div", { class: "zhead" }, h("b", {}, u.name), u.size ? h("span", { class: "faint" }, " · " + fmtB(u.size)) : null,
      h("span", { class: "tag bad" }, "sin recuperar")),
    h("div", { class: "zwhy" }, u.reason),
    u.fix ? h("div", { class: "zfix" }, h("b", {}, "Qué hacer: "), u.fix) : null,
    zeroedAction(u));
}

function renderZeroed(box, st) {
  const z = st.zeroed;
  if (!box._z) {
    const stages = h("div", { class: "zstages" });
    const info = h("div", { class: "muted", style: { marginTop: "6px" } });
    const logBody = h("div", { class: "zlog mono" });
    const log = h("details", { class: "zlogbox", open: true }, h("summary", {}, "Registro detallado del proceso"), logBody);
    const result = h("div");
    box.replaceChildren(stages, info, log, result);
    box._z = { stages, info, logBody, log, result, total: -1, stick: true };
    logBody.addEventListener("scroll", () => { box._z.stick = logBody.scrollTop + logBody.clientHeight >= logBody.scrollHeight - 8; });
  }
  const P = box._z;
  const cur = ZSTAGES.findIndex((x) => x[0] === z.stage);
  P.stages.replaceChildren(...ZSTAGES.map(([k, label], i) => h("span", { class: "zstage" + (i < cur || st.done ? " done" : i === cur ? " on" : "") },
    (i < cur || st.done ? "✓ " : i === cur ? "● " : "") + label)));
  const vol = (z.volumes || [])[0];
  P.info.replaceChildren(...[
    vol ? `Volumen «${vol.label || "sin nombre"}» (${vol.fstype === "?" ? "sin sistema de archivos reconocido" : vol.fstype.toUpperCase()}) · ${vol.files} archivos · ${vol.mode === "bloques" ? "bloques" : "clusters"} de ${fmtB(vol.cluster_size)}${vol.bitmap ? " · " + fmtB(vol.free_clusters * vol.cluster_size) + " libres" : ""}` : "",
    z.mhl_files.length ? ` · ${zerosPlural(z.mhl_files.length, "manifiesto leído", "manifiestos leídos")} (${z.mhl_files.reduce((a, m) => a + m.entries, 0)} entradas)` : "",
    z.range ? ` · rango de clusters ${z.range[0]}–${z.range[1]}` : z.search === "all" ? " · búsqueda en todo el volumen" : z.search === "near" ? " · búsqueda junto a los demás archivos" : "",
  ].filter(Boolean));
  if (P.total !== z.events_total) {
    P.total = z.events_total;
    P.logBody.replaceChildren(...z.events.map((e) => h("div", { class: "ev " + e.level }, h("span", { class: "faint" }, String(e.t).padStart(6) + "s "), e.msg)));
    if (P.stick) P.logBody.scrollTop = P.logBody.scrollHeight;
  }
  const kids = [];
  z.mhl_errors.forEach((e) => kids.push(h("div", { class: "notice warn", style: { marginTop: "6px" } }, "Manifiesto ilegible: " + e)));
  if (!z.xxhsum && st.done) kids.push(h("div", { class: "notice warn", style: { marginTop: "6px" } }, "xxhsum no está instalado: los clips con hash xxHash64 no se pueden comprobar. Instálalo con «brew install xxhash»."));
  if (st.done && !st.error) {
    P.log.open = z.unresolved.length > 0 || !z.resolved;
    if (z.summary) kids.push(h("div", { class: "notice", style: { marginTop: "6px" } }, z.summary));
    else if (z.resolved) kids.push(h("div", { class: "notice " + (z.unresolved.length ? "warn" : "ok"), style: { marginTop: "8px" } },
      h("b", {}, `${z.resolved} de ${z.targets} clips localizados. `),
      "Siguiente paso: marca los clips del grupo «Rescatados (MHL)» en la lista y pulsa Recuperar, eligiendo OTRO disco como destino. " +
      "Cada clip se vuelve a comprobar contra el hash del MHL al copiarlo; el que no coincida se guarda como «_NO_VERIFICADO» para que no pase por una copia buena."));
    if (z.survey && z.survey.sampled) kids.push(h("div", { class: "muted", style: { marginTop: "6px" } },
      `Comprobación del espacio libre: ${z.survey.data} de ${z.survey.sampled} muestras con datos, ${z.survey.zeros} a ceros.`));
  }
  z.unresolved.forEach((u) => kids.push(zeroedCard(u)));
  P.result.replaceChildren(...kids);
}

function renderPreflight(box, d, onStart) {
  const kind = { listo: "ok", parcial: "warn", sin_mhl: "warn", nada: "", error: "bad" }[d.verdict] || "";
  const kids = [h("div", { class: "notice " + kind }, d.advice || "")];
  if (d.volume) kids.push(h("div", { class: "muted", style: { marginTop: "8px" } },
    `Volumen «${d.volume.label || "sin nombre"}» (${d.volume.fstype === "?" ? "sin sistema de archivos reconocido" : d.volume.fstype.toUpperCase()}) · ${d.volume.files} archivos · ${d.volume.mode === "bloques" ? "búsqueda por bloques" : "clusters de " + fmtB(d.volume.cluster_size)}` +
    (d.volume.free_bytes != null ? ` · ${fmtB(d.volume.free_bytes)} libres` : "") +
    ` · ${zerosPlural(d.mhl_files.length, "manifiesto leído", "manifiestos leídos")}`));
  d.mhl_errors.forEach((e) => kids.push(h("div", { class: "notice warn", style: { marginTop: "6px" } }, "Manifiesto ilegible: " + e)));
  if (d.targets.length) {
    kids.push(h("table", { class: "ztable" },
      h("thead", {}, h("tr", {}, h("th", {}, "Archivo a rescatar"), h("th", {}, "Carpeta"), h("th", { class: "num" }, "Tamaño esperado"), h("th", {}, "MHL"))),
      h("tbody", {}, ...d.targets.slice(0, 200).map((t) => h("tr", {},
        h("td", { class: "name" }, t.name), h("td", { class: "muted", title: t.folder }, t.folder.split("/").slice(-2).join("/")),
        h("td", { class: "num" }, t.size ? fmtB(t.size) : "—"),
        h("td", {}, t.has_mhl ? h("span", { class: "tag ok", title: t.hash_only ? "El manifiesto no trae tamaño: se reconoce solo por su hash" : "" }, "✓ " + t.algo + (t.hash_only ? " · solo hash" : "")) : h("span", { class: "tag bad" }, "falta"),
          t.absent ? h("span", { class: "tag warn", style: { marginLeft: "4px" }, title: "Figura en el manifiesto pero no en el disco" }, "ausente") : null))))));
    if (d.targets.length > 200) kids.push(h("div", { class: "muted" }, `… y ${d.targets.length - 200} más`));
  }
  d.unresolved.forEach((u) => kids.push(zeroedCardPre(u)));
  d.warnings.forEach((w) => kids.push(h("div", { class: "notice warn", style: { marginTop: "6px" } }, w)));
  box.replaceChildren(...kids);
}

function zeroedCardPre(u) {
  return h("div", { class: "zcard" }, h("div", { class: "zhead" }, h("b", {}, u.name)), h("div", { class: "zwhy" }, u.reason),
    u.fix ? h("div", { class: "zfix" }, h("b", {}, "Qué hacer: "), u.fix) : null);
}

VIEWS.zeroed = (v) => {
  let target = null, wide = false, pre = null, busy = false;
  const extra = [];
  const list = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  const mhlBox = h("div", { class: "chips" });
  const diag = h("div", { class: "zdiag" }, h("div", { class: "muted" }, "Elige primero el disco o volumen de arriba."));
  const chosen = h("span", { class: "muted" }, "Elige un disco, un volumen o una imagen de disco");
  const startBtn = h("button", { class: "primary", disabled: true }, "Iniciar el rescate");
  const missingChk = h("input", { type: "checkbox", onchange: () => analyze() });
  const rFrom = h("input", { type: "number", min: 2, placeholder: "cluster inicial", style: { width: "130px" } });
  const rTo = h("input", { type: "number", min: 2, placeholder: "cluster final", style: { width: "130px" } });

  const analyze = async () => {
    if (!target || busy) return;
    busy = true; pre = null; startBtn.disabled = true;
    diag.replaceChildren(h("div", { class: "muted" }, h("span", { class: "spin" }), " Analizando el volumen (solo lectura)…"));
    try {
      pre = await api("zeroed/preflight", { path: target.path, mhl: extra, include_missing: missingChk.checked });
      renderPreflight(diag, pre);
      startBtn.disabled = !["listo", "parcial"].includes(pre.verdict);
    } catch (e) { diag.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
    busy = false;
  };
  const drawMhl = () => {
    mhlBox.replaceChildren(...extra.map((p) => h("span", { class: "chip on", title: p + " · clic para quitar",
      onclick: () => { extra.splice(extra.indexOf(p), 1); drawMhl(); analyze(); } }, p.split("/").pop() + " ✕")));
  };
  const addMhl = async (mode, title) => {
    const p = await pickPath({ mode, show: "manifest", title, start: S.env.home });
    if (p && !extra.includes(p)) { extra.push(p); drawMhl(); analyze(); }
  };
  const seg = h("div", { class: "seg" },
    h("button", { class: "on", onclick: () => { wide = false; seg.children[0].className = "on"; seg.children[1].className = ""; } }, "Junto a los demás archivos (rápido)"),
    h("button", { onclick: () => { wide = true; seg.children[1].className = "on"; seg.children[0].className = ""; } }, "Todo el volumen (lento)"));
  startBtn.addEventListener("click", () => {
    const body = { path: target.path, label: ZNAME + " · " + target.label, mode: "zeroed", mhl: extra, search: wide ? "all" : "near", include_missing: missingChk.checked };
    const a = parseInt(rFrom.value, 10), b = parseInt(rTo.value, 10);
    if (rFrom.value || rTo.value) {
      if (!(a >= 2 && b >= a)) { toast("Rango de clusters no válido: indica inicio y fin (el fin no puede ser menor que el inicio).", "bad"); return; }
      body.range = [a, b];
    }
    S.zeroedBody = body;
    startScan(body, "zeroed");
  });
  const select = (t) => {
    target = t;
    chosen.replaceChildren(h("b", {}, t.label));
    analyze();
  };
  diskPicker(list, { onSelect: select });
  const openImage = h("button", { onclick: async () => {
    const p = await pickPath({ mode: "file", show: "images", title: "Imagen de disco (.img, .dd, .raw, .dmg)" });
    if (p) select({ path: p, label: p.split("/").pop() });
  } }, "Abrir imagen de disco…");
  const step = (n, title, ...body) => h("section", { class: "zstep" }, h("div", { class: "znum" }, String(n)), h("div", { class: "zbody" }, h("h3", {}, title), ...body));

  v.append(page(ZNAME,
    "Para cualquier disco, tarjeta o imagen donde archivos que se grabaron completos aparecen con 0 bytes, o han desaparecido, pero sus datos siguen en el disco " +
    "(entrada del directorio reiniciada, copia interrumpida, volumen dañado). Funciona con exFAT y FAT12/16/32 usando su tabla de asignación (incluidos los archivos fragmentados) " +
    "y con cualquier otro sistema, o sin sistema legible, buscando por bloques. Cada archivo se reconoce con el manifiesto de hashes del rodaje o de la copia " +
    "(MHL, .md5, .sha1, .sha256, .xxh64) y se comprueba contra ese hash. El disco de origen solo se lee: nunca se escribe en él.",
    step(1, "Elige el disco o volumen dañado", list, h("div", { class: "row", style: { marginTop: "10px" } }, openImage, chosen)),
    step(2, "Manifiesto de hashes", h("div", { class: "muted" }, "RecoFix usa los manifiestos (.mhl, .md5, .sha1, .sha256…) que hay en el propio volumen. Si están en otro sitio (otro disco, carpeta de entrega), añádelos aquí. Sin manifiesto no hay forma de reconocer un archivo."),
      h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "small", onclick: () => addMhl("file", "Manifiesto de hashes (.mhl, .md5, .sha1, .sha256…)") }, "Añadir manifiesto…"),
        h("button", { class: "small", onclick: () => addMhl("dir", "Carpeta con manifiestos de hashes") }, "Añadir carpeta…"), mhlBox),
      h("label", { class: "chk", style: { marginTop: "10px" } }, missingChk, "Buscar también los archivos del manifiesto que no aparecen en el disco (perdidos, no solo vacíos)")),
    step(3, "Diagnóstico previo", diag, h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "small", onclick: analyze }, "Analizar de nuevo"))),
    step(4, "Rescate", h("div", { class: "col", style: { gap: "6px" } }, h("small", { class: "muted" }, "Dónde buscar los datos (en volúmenes sin tabla de asignación siempre se recorre todo)"), seg),
      h("details", { class: "zadv" }, h("summary", {}, "Opciones avanzadas: limitar a un rango de clusters o bloques"),
        h("div", { class: "row", style: { margin: "8px 0" } }, rFrom, "–", rTo,
          h("span", { class: "muted" }, "Útil si ya sabes en qué zona del disco están los archivos (p. ej. de una ejecución anterior)."))),
      h("div", { class: "opts" }, h("span", { class: "spacer" }), startBtn))));
};

// ------------------------------------------------ results

function openScan(id) {
  S.scan = { id, folder: null, filt: { group: "", category: "", q: "", sort: "", desc: "", status: "", ext: "", size_min: "", size_max: "", date_from: "", date_to: "" },
    offset: 0, limit: 200, sel: new Set(), cur: null, view: "list", tab: "path" };
  go("results");
}

VIEWS.results = (v) => {
  const sc = S.scan;
  if (!sc) return go("home");
  const bar = h("i", { style: { width: "0%" } });
  const phase = h("div", { class: "muted" });
  const statFound = h("b", {}, "0"), statSpeed = h("b", {}, "—"), statEta = h("b", {}, "—"), statBad = h("b", {}, "0");
  const pauseBtn = h("button", { onclick: async () => { const st = await api("scan/" + sc.id); await api("scan/" + sc.id + "/" + (st.paused ? "resume" : "pause"), {}); } }, "Pausar");
  const stopBtn = h("button", { class: "danger", onclick: async () => { if (confirm("¿Detener el escaneo? Podrás reanudarlo desde el historial si lo guardas.")) await api("scan/" + sc.id + "/cancel", {}); } }, "Detener");
  const saveBtn = h("button", { onclick: async () => { try { const r = await api("scan/" + sc.id + "/save", {}); toast("Guardado en " + r.path, "ok"); } catch (e) { toast(e.message, "bad"); } } }, "Guardar escaneo");
  const title = h("b", {}, "Escaneo");
  const lost = h("div");
  const zbox = h("div");
  const scanbar = h("div", { class: "scanbar" },
    h("div", { class: "prog" }, h("div", { class: "row" }, title, phase), h("div", { class: "bar", style: { marginTop: "6px" } }, bar), lost, zbox),
    h("div", { class: "stat" }, statFound, h("small", {}, "encontrados")), h("div", { class: "stat" }, statSpeed, h("small", {}, "velocidad")),
    h("div", { class: "stat" }, statEta, h("small", {}, "restante")), h("div", { class: "stat" }, statBad, h("small", {}, "sectores ilegibles")),
    pauseBtn, stopBtn, saveBtn);

  // tree
  const treeEl = h("div", { class: "tree" });
  const tabs = h("div", { class: "tab seg" },
    h("button", { class: sc.tab === "path" ? "on" : "", onclick: () => { sc.tab = "path"; sc.folder = null; renderTree(); loadItems(); tabs.children[0].className = "on"; tabs.children[1].className = ""; } }, "Ruta"),
    h("button", { class: sc.tab === "type" ? "on" : "", onclick: () => { sc.tab = "type"; sc.folder = null; renderTree(); loadItems(); tabs.children[1].className = "on"; tabs.children[0].className = ""; } }, "Tipo"));
  const treeBody = h("div");
  treeEl.append(tabs, treeBody);
  let lastFacets = { groups: {}, categories: {} };
  async function renderTree() {
    if (sc.tab === "type") {
      const f = lastFacets;
      const node = (label, count, on, click) => h("div", { class: "node" + (on ? " on" : ""), onclick: click }, h("span", { class: "tw" }), h("span", { class: "n" }, label), h("span", { class: "c" }, count));
      treeBody.replaceChildren(
        node("Todos", Object.values(f.categories).reduce((a, b) => a + b, 0), !sc.filt.category && !sc.filt.group, () => { sc.filt.category = ""; sc.filt.group = ""; reload(); }),
        h("h6", { class: "faint", style: { margin: "10px 6px 4px" } }, "TIPO"),
        ...Object.keys(CAT).filter((c) => f.categories[c]).map((c) => node(CAT[c], f.categories[c], sc.filt.category === c, () => { sc.filt.category = c; reload(); })),
        h("h6", { class: "faint", style: { margin: "10px 6px 4px" } }, "ORIGEN"),
        ...Object.keys(GROUP).filter((g) => f.groups[g]).map((g) => node(GROUP[g], f.groups[g], sc.filt.group === g, () => { sc.filt.group = sc.filt.group === g ? "" : g; reload(); })));
      return;
    }
    const mk = async (folder, depth, host) => {
      const kids = await api(`scan/${sc.id}/tree?` + qs({ folder }));
      host.replaceChildren(...kids.map((k) => {
        const sub = h("div");
        let open = false;
        const n = h("div", { class: "node" + (sc.folder === k.path ? " on" : ""), style: { paddingLeft: 6 + depth * 12 + "px" } },
          h("span", { class: "tw" }, "▸"), h("span", { class: "n", title: k.path }, k.name), h("span", { class: "c" }, k.count));
        n.onclick = async (e) => {
          sc.folder = k.path; sc.offset = 0;
          treeEl.querySelectorAll(".node.on").forEach((x) => x.classList.remove("on")); n.classList.add("on");
          if (!open) { open = true; n.firstChild.textContent = "▾"; await mk(k.path, depth + 1, sub); }
          else if (e.target === n.firstChild) { open = false; n.firstChild.textContent = "▸"; sub.replaceChildren(); }
          loadItems();
        };
        return h("div", {}, n, sub);
      }));
    };
    const all = h("div", { class: "node" + (sc.folder == null ? " on" : ""), onclick: () => { sc.folder = null; renderTree(); loadItems(); } },
      h("span", { class: "tw" }), h("span", { class: "n" }, "Todo el escaneo"));
    const host = h("div");
    treeBody.replaceChildren(all, host);
    await mk("", 0, host);
  }

  // filters
  const F = sc.filt;
  const search = h("input", { type: "search", placeholder: "Buscar nombre o extensión", value: F.q });
  let st;
  search.oninput = () => { clearTimeout(st); st = setTimeout(() => { F.q = search.value; reload(); }, 300); };
  const sel = (key, opts) => {
    const s = h("select", { onchange: () => { F[key] = s.value; reload(); } }, ...opts.map(([v, t]) => h("option", { value: v, selected: F[key] === v ? true : null }, t)));
    return s;
  };
  const sizeSel = h("select", { onchange: () => { const [a, b] = sizeSel.value.split("-"); F.size_min = a; F.size_max = b; reload(); } },
    ...[["-", "Cualquier tamaño"], ["0-1048576", "< 1 MB"], ["1048576-104857600", "1–100 MB"], ["104857600-1073741824", "100 MB–1 GB"], ["1073741824-", "> 1 GB"]].map(([v, t]) => h("option", { value: v }, t)));
  const dateFrom = h("input", { type: "date", onchange: () => { F.date_from = dateFrom.value; reload(); } });
  const dateTo = h("input", { type: "date", onchange: () => { F.date_to = dateTo.value; reload(); } });
  const viewSeg = h("div", { class: "seg" },
    h("button", { class: sc.view === "list" ? "on" : "", onclick: () => { sc.view = "list"; viewSeg.children[0].className = "on"; viewSeg.children[1].className = ""; loadItems(); } }, "Lista"),
    h("button", { class: sc.view === "grid" ? "on" : "", onclick: () => { sc.view = "grid"; viewSeg.children[1].className = "on"; viewSeg.children[0].className = ""; loadItems(); } }, "Miniaturas"));
  const filters = h("div", { class: "filters" }, search,
    sel("status", [["", "Todos los estados"], ["deleted", "Borrados / perdidos"], ["existing", "Existentes"]]),
    sel("category", [["", "Todos los tipos"], ...Object.entries(CAT).filter(([k]) => k !== "system").map(([k, t]) => [k, t])]),
    sizeSel, h("span", { class: "muted" }, "Fecha"), dateFrom, dateTo, h("span", { class: "spacer" }), viewSeg);

  const table = h("div", { class: "table" });
  const pager = h("div", { class: "pager" });
  const pv = h("div", { class: "preview" }, h("div", { class: "empty" }, "Selecciona un archivo para ver la vista previa."));
  const footSel = h("span", { class: "muted" }, "Nada seleccionado");
  const recBtn = h("button", { class: "primary", onclick: () => recoverDialog() }, "Recuperar");
  const footer = h("div", { class: "footer" }, footSel, h("span", { class: "spacer" }),
    h("button", { onclick: () => selectAllFiltered() }, "Seleccionar todo lo filtrado"),
    h("button", { class: "ghost", onclick: () => { sc.sel.clear(); loadItems(); } }, "Quitar selección"), recBtn);
  v.append(h("div", { class: "page full" }, scanbar,
    h("div", { class: "results" }, treeEl, h("div", { class: "listwrap" }, filters, table, pager), pv), footer));

  let total = 0, totalBytes = 0, lastItems = [];
  const params = () => Object.assign({}, F, { offset: sc.offset, limit: sc.limit }, sc.folder != null && sc.tab === "path" ? { folder: sc.folder } : {});
  function reload() { sc.offset = 0; loadItems(); }
  function updateFooter() {
    footSel.textContent = sc.sel.size ? `${sc.sel.size} seleccionados` : `Nada seleccionado · ${total} archivos en la vista (${fmtB(totalBytes)})`;
    recBtn.textContent = sc.sel.size ? `Recuperar ${sc.sel.size}` : "Recuperar todo lo filtrado";
  }
  async function selectAllFiltered() {
    let off = 0;
    while (true) {
      const d = await api(`scan/${sc.id}/items?` + qs(Object.assign(params(), { offset: off, limit: 1000 })));
      d.items.forEach((i) => sc.sel.add(i.id));
      off += 1000;
      if (off >= d.total) break;
    }
    loadItems();
  }
  async function loadItems() {
    let d;
    try { d = await api(`scan/${sc.id}/items?` + qs(params())); } catch (e) { return; }
    total = d.total; totalBytes = d.total_bytes; lastItems = d.items; lastFacets = d.facets;
    if (sc.tab === "type") renderTree();
    if (sc.view === "grid") {
      table.replaceChildren(h("div", { class: "gridview" }, ...d.items.map((it) => {
        const im = h("div", { class: "im" }, h("span", { class: "fic " + it.category }, (it.ext || "?").slice(0, 4).toUpperCase()));
        if (it.category === "photo" || it.category === "video") {
          fetch(`/api/scan/${sc.id}/preview/${it.id}`, { headers: { "X-Token": TOKEN } }).then((r) => r.ok && r.headers.get("content-type").startsWith("image") ? r.blob() : null)
            .then((b) => { if (b) { im.style.backgroundImage = `url(${URL.createObjectURL(b)})`; im.replaceChildren(); } }).catch(() => {});
        }
        const cb = h("input", { type: "checkbox", checked: sc.sel.has(it.id) ? true : null, onclick: (e) => { e.stopPropagation(); e.target.checked ? sc.sel.add(it.id) : sc.sel.delete(it.id); updateFooter(); } });
        return h("div", { class: "thumb" + (sc.cur === it.id ? " cur" : ""), onclick: () => showItem(it) }, im, h("div", { class: "cap" }, cb, " ", it.name));
      })));
    } else {
      const th = (k, t, cls) => h("th", { class: cls, onclick: () => { F.desc = F.sort === k && F.desc !== "1" ? "1" : ""; F.sort = k; loadItems(); } }, t, F.sort === k ? (F.desc ? " ↓" : " ↑") : "");
      const all = h("input", { type: "checkbox", onclick: (e) => { d.items.forEach((i) => e.target.checked ? sc.sel.add(i.id) : sc.sel.delete(i.id)); loadItems(); } });
      table.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, all), th("name", "Nombre"), th("size", "Tamaño", "num"), th("mtime", "Fecha"), th("ext", "Tipo"), h("th", {}, "Estado"), h("th", {}, "Ruta"))),
        h("tbody", {}, ...d.items.map((it) => {
          const cb = h("input", { type: "checkbox", checked: sc.sel.has(it.id) ? true : null, onclick: (e) => { e.stopPropagation(); e.target.checked ? sc.sel.add(it.id) : sc.sel.delete(it.id); updateFooter(); } });
          return h("tr", { class: "it" + (sc.cur === it.id ? " cur" : ""), onclick: () => showItem(it) },
            h("td", {}, cb), h("td", { class: "name", title: it.name }, h("span", { class: "fic " + it.category }, (it.ext || "?").slice(0, 4).toUpperCase()), it.name),
            h("td", { class: "num" }, fmtB(it.size)), h("td", { class: "muted" }, it.mtime || "—"), h("td", {}, (it.ext || "").toUpperCase()),
            h("td", {}, statusTags(it)), h("td", { class: "muted", title: it.path }, it.path || "—"));
        }))));
      if (!d.items.length) table.append(h("div", { class: "empty" }, "Sin resultados con estos filtros."));
    }
    const pages = Math.max(1, Math.ceil(total / sc.limit)), cur = Math.floor(sc.offset / sc.limit) + 1;
    pager.replaceChildren(h("span", { class: "muted" }, `${total} archivos · ${fmtB(totalBytes)}`), h("span", { class: "spacer" }),
      h("button", { class: "small", disabled: cur <= 1 || null, onclick: () => { sc.offset -= sc.limit; loadItems(); } }, "‹"),
      h("span", {}, `${cur} / ${pages}`),
      h("button", { class: "small", disabled: cur >= pages || null, onclick: () => { sc.offset += sc.limit; loadItems(); } }, "›"));
    updateFooter();
  }
  function statusTags(it) {
    const t = [h("span", { class: "tag " + (it.group === "existing" ? "" : it.group === "raw" ? "info" : it.group === "zeroed" ? "acc" : "warn") }, GROUP[it.group].split(" ")[0])];
    if (!it.complete) t.push(h("span", { class: "tag bad" }, "incompleto"));
    if (it.fragments > 1) t.push(h("span", { class: "tag" }, it.fragments + " frag."));
    return t;
  }
  async function showItem(it) {
    sc.cur = it.id;
    table.querySelectorAll(".cur").forEach((x) => x.classList.remove("cur"));
    const box = h("div", { class: "pv" }, h("span", { class: "spin" }));
    const meta = Object.entries(it.meta || {}).map(([k, val]) => [k, Array.isArray(val) ? val.join(", ")
      : val && typeof val === "object" ? Object.entries(val).map(([a, b]) => `${a}: ${b}`).join(" · ") : String(val)]);
    const rows = [["Nombre", it.name], ["Tamaño", fmtB(it.size) + ` (${it.size} B)`], ["Origen", GROUP[it.group]], ["Fecha", it.mtime || "—"],
      ["Ruta", it.path || "—"], ["Offset", it.start >= 0 ? it.start : "—"], ["Fragmentos", it.fragments], ["Formato", it.fmt || it.ext], ["Sistema", it.fs || "—"], ...meta];
    const actions = h("div", { class: "row", style: { marginTop: "12px" } },
      h("button", { class: "primary small", onclick: () => recoverDialog([it.id]) }, "Recuperar este archivo"));
    if (["mp4", "mov", "m4v", "3gp"].includes(it.ext) && it.start >= 0 && !it.complete) {
      actions.append(h("button", { class: "small", onclick: () => reassembleDialog(it) }, "Reensamblar vídeo"));
    }
    pv.replaceChildren(box, actions, it.note ? h("div", { class: "notice" + (/sobrescri|ceros|incomplet|no corresponde|falta|interrump|hueco|no coincid/i.test(it.note) ? " warn" : ""), style: { marginTop: "10px" } }, it.note) : null,
      h("dl", { class: "kv" }, ...rows.flatMap(([k, val]) => [h("dt", {}, k), h("dd", { class: k === "Offset" ? "mono" : "" }, val)])));
    try {
      const r = await fetch(`/api/scan/${sc.id}/preview/${it.id}`, { headers: { "X-Token": TOKEN } });
      if (!r.ok) throw new Error();
      const ct = r.headers.get("content-type") || "";
      if (ct.startsWith("image")) box.replaceChildren(h("img", { src: URL.createObjectURL(await r.blob()) }));
      else box.replaceChildren(h("pre", {}, await r.text()));
    } catch (e) { box.replaceChildren(h("span", { class: "muted" }, "Sin vista previa — recupera el archivo para abrirlo.")); }
  }
  async function recoverDialog(ids) {
    const useIds = ids || (sc.sel.size ? [...sc.sel] : null);
    const dest = await pickPath({ mode: "dir", title: "Destino de la recuperación (otro disco)", start: "/Volumes", allowNew: true });
    if (!dest) return;
    try {
      const body = useIds ? { scan: sc.id, ids: useIds, dest } : { scan: sc.id, filter: params(), dest };
      const r = await api("recover", body);
      recoverProgress(r.id, dest);
    } catch (e) { toast(e.message, "bad"); }
  }
  async function reassembleDialog(it) {
    const dest = await pickPath({ mode: "dir", title: "Guardar el clip reensamblado en (otro disco)", allowNew: true });
    if (!dest) return;
    const cl = prompt("Tamaño de clúster del volumen en bytes (exFAT de tarjeta: 32768–131072; FAT32: 32768)", "32768");
    if (!cl) return;
    try {
      const r = await api("reassemble", { scan: sc.id, item: it.id, dest, cluster: parseInt(cl, 10) });
      const msg = h("div", {}, h("span", { class: "spin" }), " Reensamblando…");
      const close = modal("Reensamblar " + it.name, msg, [h("button", { onclick: () => close() }, "Cerrar")]);
      const st = await waitJob(r.id, (s) => { msg.replaceChildren(h("div", { class: "bar" }, h("i", { style: { width: 100 * s.progress + "%" } })), h("p", { class: "muted" }, s.phase)); });
      if (st.error) msg.replaceChildren(h("div", { class: "notice bad" }, st.error));
      else {
        const res = st.result;
        msg.replaceChildren(h("div", { class: "notice " + (res.ok ? "ok" : "warn") }, `${res.frames_ok} de ${res.frames} fotogramas localizados en ${res.fragments} fragmentos.`),
          h("p", {}, res.out), res.check ? h("p", { class: "muted" }, res.check.playable ? "Se reproduce correctamente." : "La comprobación de reproducción dio errores.") : null,
          h("button", { onclick: () => api("reveal", { path: res.out }) }, "Mostrar en Finder"));
      }
    } catch (e) { toast(e.message, "bad"); }
  }

  // live status
  let lastFound = -1;
  every(1000, async () => {
    let st;
    try { st = await api("scan/" + sc.id); } catch (e) { return; }
    title.textContent = st.label || "Escaneo";
    bar.style.width = (100 * st.progress).toFixed(1) + "%";
    phase.textContent = " · " + st.phase + (st.current_path ? " · " + st.current_path : "");
    statFound.textContent = st.found; statSpeed.textContent = st.speed ? fmtB(st.speed) + "/s" : "—";
    statEta.textContent = st.done ? "—" : fmtT(st.eta); statBad.textContent = st.bad_sectors;
    pauseBtn.textContent = st.paused ? "Reanudar" : "Pausar";
    pauseBtn.disabled = stopBtn.disabled = st.done;
    lost.replaceChildren(...(st.lost_partitions || []).map((p) => h("span", { class: "tag acc", style: { marginTop: "6px", marginRight: "6px" } },
      `Partición perdida ${p.fstype.toUpperCase()} · ${fmtB(p.size)} @ ${fmtB(p.offset)}`)));
    if (st.zeroed) renderZeroed(zbox, st);
    if (st.error) phase.replaceChildren(h("span", { style: { color: "var(--bad)" } }, " · " + st.error));
    if (st.found !== lastFound) {
      const first = lastFound < 0;
      lastFound = st.found;
      if (first || st.done || st.found < 2000) { loadItems(); if (sc.tab === "path") renderTree(); }
    }
    if (st.done && !sc.notified) {
      sc.notified = true;
      loadItems(); renderTree();
      if (!st.error) toast(`Escaneo terminado: ${st.found} archivos`, "ok");
    }
  });
}

function recoverProgress(id, dest) {
  const body = h("div", {});
  const close = modal("Recuperando", body, [h("button", { onclick: () => api("reveal", { path: dest }) }, "Abrir destino"), h("button", { class: "danger", onclick: () => api("job/" + id + "/cancel", {}) }, "Cancelar"), h("button", { onclick: () => close() }, "Cerrar")]);
  waitJob(id, (st) => {
    const ok = (st.results || []).filter((r) => r.ok).length;
    body.replaceChildren(...[h("div", { class: "bar" }, h("i", { style: { width: (100 * st.progress).toFixed(1) + "%" } })),
      h("p", {}, `${fmtB(st.done_bytes)} de ${fmtB(st.total_bytes)} · ${st.speed ? fmtB(st.speed) + "/s" : ""}`),
      h("p", { class: "muted mono" }, st.current || ""),
      st.error ? h("div", { class: "notice bad" }, st.error) : null,
      st.done && !st.error ? h("div", { class: "notice " + (ok === st.count ? "ok" : "warn") }, `${ok} de ${st.count} archivos recuperados en ${st.dest}. Informe JSON/CSV con MD5 junto a los archivos.`,
        (st.results || []).some((r) => r.mhl) ? ` ${(st.results || []).filter((r) => r.mhl === "OK").length} verificados con su MHL (hash idéntico al original).` : "") : null,
      st.done && !st.error && (st.results || []).some((r) => r.estado === "no_verificado") ? h("div", { class: "notice warn" },
        h("b", {}, `${(st.results || []).filter((r) => r.estado === "no_verificado").length} clips NO coinciden con el hash del MHL. `),
        "Se guardaron con el sufijo «_NO_VERIFICADO» (nunca con el nombre original) porque les faltan datos que ya se sobrescribieron en el disco. " +
        "Sirven para revisar o para un uso provisional, no como copia de entrega: para el clip íntegro hace falta otra copia (tarjeta de cámara u otro disco del rodaje).") : null,
      st.done && !st.error && (st.results || []).some((r) => r.estado === "sin_mhl") ? h("div", { class: "notice warn" },
        "Algunos clips se copiaron sin MHL: no se puede garantizar que sean idénticos al original.") : null,
      ...(st.results || []).filter((r) => !r.ok || r.note).slice(-20).map((r) => h("div", { class: "muted" }, r.name + ": " + (r.note || "error")))].filter(Boolean));
  });
}

// ------------------------------------------------ trash / iCloud

function trashView(v, onlyCloud) {
  const tbl = h("div", { class: "tbl" }, h("div", { class: "empty" }, h("span", { class: "spin" }), " Leyendo papeleras…"));
  const errs = h("div");
  const sel = new Set();
  let items = [];
  const restoreOrig = h("button", { class: "primary", disabled: true, onclick: () => restore(true) }, "Restaurar a su ubicación");
  const restoreTo = h("button", { disabled: true, onclick: () => restore(false) }, "Recuperar en…");
  const upd = () => { restoreOrig.disabled = restoreTo.disabled = !sel.size; };
  async function restore(orig) {
    let dest = null;
    if (!orig) { dest = await pickPath({ mode: "dir", allowNew: true, start: S.env.home + "/Desktop" }); if (!dest) return; }
    const r = await api("trash/restore", { paths: [...sel], dest, to_original: orig });
    const ok = r.filter((x) => x.ok).length;
    toast(`${ok} de ${r.length} restaurados`, ok === r.length ? "ok" : "bad");
    r.filter((x) => !x.ok).forEach((x) => toast(x.path.split("/").pop() + ": " + x.note, "bad"));
  }
  v.append(page(onlyCloud ? "iCloud Drive · Eliminados recientemente" : "Papelera",
    onlyCloud ? "Archivos borrados de iCloud Drive en este Mac (se conservan 30 días). Para lo que ya se vació, escanea el disco interno."
      : "Papelera del usuario, de cada volumen externo y de iCloud Drive. Se copia (no se mueve) y se restaura en la carpeta original que registró Finder.",
    errs, tbl, h("div", { class: "opts" }, h("span", { class: "muted" }, "¿Ya vaciaste la papelera? Usa «Discos y ubicaciones» › disco interno."), h("span", { class: "spacer" }), restoreTo, restoreOrig)));
  api("trash").then((d) => {
    items = d.items.filter((i) => !onlyCloud || i.bin.startsWith("iCloud"));
    errs.replaceChildren(...d.errors.map((e) => h("div", { class: "notice warn", style: { marginBottom: "10px" } }, e)));
    if (!items.length) { tbl.replaceChildren(h("div", { class: "empty" }, "No hay elementos.")); return; }
    tbl.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}), h("th", {}, "Nombre"), h("th", { class: "num" }, "Tamaño"), h("th", {}, "Enviado"), h("th", {}, "Ubicación original"), h("th", {}, "Papelera"))),
      h("tbody", {}, ...items.map((it) => h("tr", { class: "it" },
        h("td", {}, h("input", { type: "checkbox", onchange: (e) => { e.target.checked ? sel.add(it.path) : sel.delete(it.path); upd(); } })),
        h("td", { class: "name" }, h("span", { class: "fic other" }, it.is_dir ? "DIR" : (it.ext || "?").slice(0, 4).toUpperCase()), it.name, it.files > 1 ? h("span", { class: "faint" }, ` (${it.files})`) : null),
        h("td", { class: "num" }, fmtB(it.size)), h("td", { class: "muted" }, it.trashed), h("td", { class: "muted", title: it.original }, it.original || "—"), h("td", {}, h("span", { class: "tag" }, it.bin)))))));
  }).catch((e) => tbl.replaceChildren(h("div", { class: "notice bad" }, e.message)));
}
VIEWS.trash = (v) => trashView(v, false);
VIEWS.icloud = (v) => trashView(v, true);

// ------------------------------------------------ Time Machine

VIEWS.tm = (v) => {
  const list = h("div", { class: "col" }, h("div", { class: "card" }, h("span", { class: "spin" }), " Consultando Time Machine…"));
  const browser = h("div");
  v.append(page("Time Machine", "Copias de seguridad e instantáneas locales de APFS. Son copias íntegras: los archivos vuelven con su nombre, carpeta y fecha exactos.", list, browser));
  api("tm").then((d) => {
    const rows = [];
    if (d.error) rows.push(h("div", { class: "notice warn" }, d.error));
    d.destinations.forEach((x) => rows.push(h("div", { class: "muted" }, `Destino: ${x.name} (${x.kind}) ${x.mount || "no montado"}`)));
    rows.push(h("h2", {}, "Copias de seguridad"));
    rows.push(d.backups.length ? h("div", { class: "grid" }, ...d.backups.map((b) => h("div", { class: "card click", onclick: () => browse(b.path, b.date) }, h("div", { class: "ico" }, ico("tm")), h("h3", {}, b.date), h("p", { class: "mono" }, b.path))))
      : h("p", { class: "muted" }, "No hay copias accesibles (conecta el disco de Time Machine)."));
    rows.push(h("h2", {}, "Instantáneas locales"));
    rows.push(d.snapshots.length ? h("div", { class: "grid" }, ...d.snapshots.map((s) => h("div", { class: "card click", onclick: async () => {
      try { const r = await api("tm/mount", { name: s.name, volume: s.volume }); browse(r.path, s.date); } catch (e) { toast(e.message, "bad"); }
    } }, h("div", { class: "ico" }, ico("tm")), h("h3", {}, s.date), h("p", {}, "Instantánea de " + s.volume))))
      : h("p", { class: "muted" }, "No hay instantáneas locales."));
    list.replaceChildren(...rows);
  }).catch((e) => list.replaceChildren(h("div", { class: "notice bad" }, e.message)));

  function browse(root, label) {
    const sel = new Set();
    let cur = root;
    const tbl = h("div", { class: "tbl" });
    const path = h("div", { class: "mono muted" });
    const q = h("input", { type: "search", placeholder: "Buscar en esta copia…" });
    const restoreBtn = h("button", { class: "primary", disabled: true, onclick: async () => {
      const dest = await pickPath({ mode: "dir", allowNew: true, start: S.env.home + "/Desktop" });
      if (!dest) return;
      const r = await api("tm/restore", { paths: [...sel], dest });
      toast(`${r.filter((x) => x.ok).length} de ${r.length} restaurados en ${dest}`, "ok");
    } }, "Restaurar seleccionados");
    const render = (entries) => {
      tbl.replaceChildren(h("table", {}, h("tbody", {}, h("tr", { class: "it", onclick: () => load(cur.replace(/\/[^/]+$/, "") || root) }, h("td", {}), h("td", {}, ".."), h("td", {}), h("td", {})),
        ...entries.map((e) => h("tr", { class: "it", ondblclick: () => e.is_dir && load(e.path) },
          h("td", {}, h("input", { type: "checkbox", onchange: (ev) => { ev.target.checked ? sel.add(e.path) : sel.delete(e.path); restoreBtn.disabled = !sel.size; } })),
          h("td", { class: "name", onclick: () => e.is_dir && load(e.path) }, ico(e.is_dir ? "folder" : "file"), e.name),
          h("td", { class: "num" }, e.is_dir ? "" : fmtB(e.size)), h("td", { class: "muted" }, fmtDate(e.mtime)))))));
    };
    const load = async (p) => {
      if (!p.startsWith(root)) p = root;
      cur = p; path.textContent = p;
      try { render(await api("tm/ls?" + qs({ path: p }))); } catch (e) { toast(e.message, "bad"); }
    };
    q.onkeydown = async (e) => {
      if (e.key !== "Enter" || !q.value.trim()) return;
      tbl.replaceChildren(h("div", { class: "empty" }, h("span", { class: "spin" }), " Buscando…"));
      const r = await api("tm/search", { root: cur, text: q.value.trim() });
      const st = await waitJob(r.id);
      render(st.result || []);
    };
    browser.replaceChildren(h("h2", {}, "Explorando copia del " + label), h("div", { class: "row", style: { marginBottom: "10px" } }, q, h("span", { class: "spacer" }), restoreBtn), path, tbl);
    load(root);
    browser.scrollIntoView({ behavior: "smooth" });
  }
};

// ------------------------------------------------ disk image

VIEWS.imagecfg = (v) => {
  let src = null;
  const list = h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" } });
  const prog = h("div");
  const createBtn = h("button", { class: "primary", disabled: true, onclick: async () => {
    const dir = await pickPath({ mode: "dir", title: "Carpeta de la imagen (otro disco con espacio suficiente)", allowNew: true });
    if (!dir) return;
    const name = (src.label || "disco").replace(/[^\w.-]+/g, "_") + "_" + new Date().toISOString().slice(0, 10) + ".img";
    try {
      const r = await api("image", { path: src.path, out: dir + "/" + name });
      const st = await waitJob(r.id, (s) => prog.replaceChildren(h("div", { class: "card" },
        h("div", { class: "row" }, h("b", {}, s.phase), h("span", { class: "spacer" }), h("span", { class: "muted" }, `${fmtB(s.done_bytes)} / ${fmtB(s.total_bytes)} · ${s.speed ? fmtB(s.speed) + "/s" : ""} · resta ${fmtT(s.eta)}`)),
        h("div", { class: "bar", style: { margin: "10px 0" } }, h("i", { style: { width: 100 * s.progress + "%" } })),
        h("span", { class: "tag " + (s.bad_sectors ? "warn" : "ok") }, s.bad_sectors + " sectores ilegibles"), " ",
        h("button", { class: "small danger", onclick: () => api("job/" + r.id + "/cancel", {}) }, "Cancelar"))));
      if (st.error) toast(st.error, "bad");
      else { toast("Imagen creada", "ok"); prog.append(h("div", { class: "opts" }, h("span", { class: "mono" }, st.out), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => startScan({ path: st.out, label: name, mode: "image" }, "imagecfg") }, "Escanear la imagen"))); }
    } catch (e) { toast(e.message, "bad"); }
  } }, "Crear imagen");
  diskPicker(list, { onSelect: (s) => { src = s; createBtn.disabled = false; } });
  v.append(page("Imagen de disco", "Para discos y tarjetas con errores: se leen una sola vez (primera pasada rápida que salta zonas dañadas, segunda pasada sector a sector) " +
    "y todo el trabajo posterior se hace sobre la copia. Se genera un mapa .map.json con los sectores ilegibles.",
    h("div", { class: "row", style: { marginBottom: "16px" } }, h("button", { onclick: async () => {
      const p = await pickPath({ mode: "file", show: "images", title: "Imagen existente (.img, .dd, .raw, .iso, .dmg)" });
      if (p) startScan({ path: p, label: p.split("/").pop(), mode: "image" }, "imagecfg");
    } }, "Escanear una imagen existente…")),
    h("h2", {}, "Crear imagen de"), list, prog, h("div", { class: "opts" }, h("span", { class: "spacer" }), createBtn)));
};

// ------------------------------------------------ NAS & Linux

VIEWS.nas = (v) => {
  const f = { host: h("input", { type: "text", placeholder: "192.168.1.20 o nas.local" }), user: h("input", { type: "text", placeholder: "admin" }),
    password: h("input", { type: "password", placeholder: "contraseña (no se guarda)" }), port: h("input", { type: "number", value: "22", style: { width: "80px" } }) };
  const found = h("div", { class: "row" });
  const out = h("div");
  const cred = () => ({ host: f.host.value.trim(), user: f.user.value.trim(), password: f.password.value, port: parseInt(f.port.value || "22", 10) });
  const discover = async () => {
    found.replaceChildren(h("span", { class: "spin" }), " Buscando en la red local…");
    try {
      const r = await api("remote/discover", { timeout: 4 });
      found.replaceChildren(...(r.length ? r.map((s) => h("button", { class: "small", onclick: () => { f.host.value = s.host; } }, `${s.name} · ${s.type}`)) : [h("span", { class: "muted" }, "No se anunció ningún servidor (escribe la IP).")]));
    } catch (e) { found.replaceChildren(h("span", { class: "muted" }, e.message)); }
  };
  const connect = async () => {
    out.replaceChildren(h("span", { class: "spin" }), " Conectando…");
    try {
      const t = await api("remote/test", cred());
      if (!t.ok) { out.replaceChildren(h("div", { class: "notice bad" }, t.error)); return; }
      const devs = await api("remote/devices", cred());
      out.replaceChildren(h("div", { class: "notice ok" }, `Conectado: ${t.system}${t.root ? " (root)" : " · se usará sudo con la misma contraseña"}`),
        h("h2", {}, "Discos del servidor"), h("div", { class: "tbl" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Dispositivo"), h("th", {}, "Tipo"), h("th", {}, "Sistema"), h("th", {}, "Etiqueta"), h("th", { class: "num" }, "Tamaño"), h("th", {}))),
          h("tbody", {}, ...devs.map((d) => h("tr", { class: "it" }, h("td", { class: "mono", style: { paddingLeft: 10 + d.depth * 16 + "px" } }, d.path), h("td", {}, d.type), h("td", {}, d.fs), h("td", {}, d.label || d.model),
            h("td", { class: "num" }, fmtB(d.size)), h("td", {}, h("button", { class: "small", onclick: () => copy(d) }, "Copiar y escanear"))))))));
    } catch (e) { out.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  const copy = async (d) => {
    const dir = await pickPath({ mode: "dir", title: `Dónde guardar la copia de ${d.path} (${fmtB(d.size)})`, allowNew: true });
    if (!dir) return;
    const name = `${cred().host}_${d.name}.img`.replace(/[^\w.-]+/g, "_");
    const r = await api("remote/image", Object.assign(cred(), { device: d.path, out: dir + "/" + name, size: d.size }));
    const p = h("div", { class: "card", style: { marginTop: "14px" } });
    out.append(p);
    const st = await waitJob(r.id, (s) => p.replaceChildren(h("div", { class: "row" }, h("b", {}, "Copiando " + d.path), h("span", { class: "spacer" }), h("span", { class: "muted" }, `${fmtB(s.done_bytes)} / ${fmtB(s.total_bytes)} · ${s.speed ? fmtB(s.speed) + "/s" : ""}`)),
      h("div", { class: "bar", style: { marginTop: "10px" } }, h("i", { style: { width: 100 * s.progress + "%" } }))));
    if (st.error) p.append(h("div", { class: "notice bad", style: { marginTop: "10px" } }, st.error));
    else startScan({ path: st.out, label: `${cred().host} · ${d.path}`, mode: "nas" }, "nas");
  };
  v.append(page("NAS y Linux", "Recupera datos de un NAS (Synology, QNAP, TrueNAS…) o de un equipo Linux por SSH. El disco remoto solo se lee: se copia a una imagen local que después se escanea.",
    h("div", { class: "card col" }, h("div", { class: "row" }, h("button", { onclick: discover }, "Buscar servidores en la red"), found),
      h("div", { class: "row" }, f.host, f.port, f.user, f.password, h("button", { class: "primary", onclick: connect }, "Conectar")),
      h("small", { class: "muted" }, "Requiere SSH activado en el NAS y un usuario con permisos de administrador (sudo).")), out));
  discover();
};

// ------------------------------------------------ crashed Mac

VIEWS.crash = (v) => {
  const vols = h("div", { class: "row" });
  const res = h("div");
  api("ls?" + qs({ path: "/Volumes", show: "none" })).then((d) => vols.replaceChildren(...d.entries.filter((e) => e.dir).map((e) =>
    h("button", { onclick: async () => {
      if (!confirm(`Se escribirán rescate.sh, LEEME.txt y la carpeta RecoFix en ${e.path}. ¿Continuar?`)) return;
      try { const r = await api("rescue", { volume: e.path }); res.replaceChildren(h("div", { class: "notice ok", style: { marginTop: "12px" } }, "Kit creado en " + r.volume + ": " + r.files.join(", "))); }
      catch (err) { res.replaceChildren(h("div", { class: "notice bad", style: { marginTop: "12px" } }, err.message)); }
    } }, e.name))));
  v.append(page("Mac que no arranca", "Prepara una unidad USB de rescate en este Mac y úsala en el que no arranca.",
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", {}, "A · Desde macOS Recovery"), h("ol", { class: "steps" },
        h("li", {}, "Crea el kit en una unidad USB (abajo)."), h("li", {}, "En el Mac roto entra en Recuperación (Apple silicon: mantén el botón de encendido; Intel: ⌘R)."),
        h("li", {}, "Utilidades › Terminal: sh /Volumes/<USB>/rescate.sh"), h("li", {}, "Copia tus carpetas o crea una imagen completa del disco en el USB."))),
      h("div", { class: "card" }, h("h3", {}, "B · Con otro Mac"), h("ol", { class: "steps" },
        h("li", {}, "Apple silicon: Recuperación › Utilidades › Compartir disco. Intel: arranca pulsando T."), h("li", {}, "Conecta ambos Macs con un cable USB-C / Thunderbolt."),
        h("li", {}, "En el Mac sano, el disco aparece en «Discos y ubicaciones»: escanéalo o crea una imagen.")))),
    h("h2", {}, "Crear kit de rescate en"), vols, res));
};

// ------------------------------------------------ repair

function repairView(v, kind) {
  const conf = {
    video: ["Reparar vídeo", "MP4, MOV, M4V, 3GP, MXF, MKV, AVI, MTS… Rápida: reconstruye el índice y las marcas de tiempo (MXF: footer y RIP). " +
      "Avanzada: para clips cuyo índice desapareció (grabación interrumpida, batería agotada); necesita un vídeo de muestra grabado con la misma cámara y ajustes."],
    photo: ["Reparar fotos", "JPEG truncados o con datos basura, JPEG con cabecera dañada (con foto de muestra de la misma cámara), PNG dañados y rescate de la vista previa incrustada en RAW (CR2, CR3, NEF, ARW, DNG, RAF…)."],
    document: ["Reparar documentos", "Word, Excel, PowerPoint, Pages, Numbers, Keynote, ODF, EPUB y ZIP se reconstruyen miembro a miembro; PDF con tabla xref nueva; SQLite con .recover; texto sin bytes nulos."],
  }[kind];
  let files = [], sample = null, mode = "quick", dest = null;
  const list = h("div", { class: "files" });
  const sampleBox = h("div", { class: "row" });
  const destLbl = h("span", { class: "muted" }, "Junto al original");
  const results = h("div");
  const renderFiles = () => list.replaceChildren(...files.map((f, i) => h("div", { class: "file" },
    h("div", {}, h("b", {}, f.split("/").pop()), h("div", { class: "muted mono" }, f)), h("button", { class: "ghost small", onclick: () => { files.splice(i, 1); renderFiles(); } }, "Quitar"))));
  const add = async () => { const p = await pickPath({ mode: "files", show: "media", start: S.env.home + "/Desktop", title: "Archivos dañados" }); if (p) { files = [...new Set([...files, ...p])]; renderFiles(); } };
  const pickSample = async () => {
    const p = await pickPath({ mode: "file", show: "media", start: S.env.home + "/Desktop", title: kind === "video" ? "Vídeo de muestra (se reproduce bien, misma cámara)" : "Foto de muestra (misma cámara)" });
    if (p) { sample = p; sampleBox.replaceChildren(h("span", { class: "tag acc" }, "Muestra: " + p.split("/").pop()), h("button", { class: "ghost small", onclick: () => { sample = null; renderSample(); } }, "Quitar")); }
  };
  const renderSample = () => sampleBox.replaceChildren(h("button", { onclick: pickSample }, kind === "video" ? "Añadir vídeo de muestra…" : "Añadir foto de muestra…"));
  renderSample();
  const seg = h("div", { class: "seg" }, h("button", { class: "on", onclick: () => setMode("quick") }, "Rápida"), h("button", { onclick: () => setMode("advanced") }, "Avanzada (con muestra)"));
  const setMode = (m) => { mode = m; seg.children[0].className = m === "quick" ? "on" : ""; seg.children[1].className = m === "advanced" ? "on" : ""; sampleBox.classList.toggle("hidden", m !== "advanced"); };
  if (kind === "video") setMode("quick");
  const run = h("button", { class: "primary", onclick: async () => {
    if (!files.length) return toast("Añade al menos un archivo", "bad");
    if (kind === "video" && mode === "advanced" && !sample) return toast("La reparación avanzada necesita un vídeo de muestra", "bad");
    run.disabled = true;
    try {
      const r = await api("repair", { kind, files, mode, sample, dest });
      await waitJob(r.id, (st) => results.replaceChildren(h("h2", {}, st.done ? "Resultado" : "Reparando… " + Math.round(100 * st.progress) + "%"),
        h("div", { class: "files" }, ...(st.results || []).map((x) => h("div", { class: "file" },
          h("div", {}, h("div", { class: "row" }, h("span", { class: "tag " + (x.ok ? "ok" : "bad") }, x.ok ? "Reparado" : "No reparado"), h("b", {}, x.file.split("/").pop())),
            h("div", { class: "muted", style: { marginTop: "4px" } }, x.ok ? x.method : x.error),
            x.detail ? h("div", { class: "faint mono", style: { marginTop: "4px" } }, Object.entries(x.detail).map(([k, val]) => `${k}: ${typeof val === "object" ? JSON.stringify(val) : val}`).join(" · ")) : null,
            x.check ? h("div", { class: "faint", style: { marginTop: "4px" } }, x.check.playable ? "✓ Se reproduce" : "Comprobación con errores: " + (x.check.errors || []).slice(0, 2).join(" ")) : null,
            x.needs_sample ? h("div", { class: "notice warn", style: { marginTop: "6px" } }, kind === "video" ? "Usa la reparación avanzada con un vídeo de muestra." : "Añade una foto de muestra de la misma cámara.") : null),
          x.out ? h("button", { class: "small", onclick: () => api("reveal", { path: x.out }) }, "Mostrar en Finder") : null)))));
    } catch (e) { toast(e.message, "bad"); }
    run.disabled = false;
  } }, "Reparar");
  v.append(page(conf[0], conf[1],
    h("div", { class: "drop", onclick: add }, h("b", {}, "Añadir archivos dañados…"), h("div", {}, "Se trabaja sobre una copia; el original no se modifica.")),
    h("div", { style: { marginTop: "12px" } }, list),
    h("div", { class: "opts" }, kind === "video" ? seg : null, kind === "photo" ? null : null, kind !== "document" ? sampleBox : null,
      h("span", { class: "spacer" }), h("span", { class: "muted" }, "Guardar en:"), destLbl,
      h("button", { class: "small", onclick: async () => { dest = await pickPath({ mode: "dir", allowNew: true, start: S.env.home + "/Desktop" }); destLbl.textContent = dest || "Junto al original"; } }, "Cambiar"), run),
    results));
}
VIEWS.rvideo = (v) => repairView(v, "video");
VIEWS.rphoto = (v) => repairView(v, "photo");
VIEWS.rdoc = (v) => repairView(v, "document");

// ------------------------------------------------ tools

VIEWS.smart = (v) => {
  const box = h("div", { class: "col" }, h("div", { class: "card" }, h("span", { class: "spin" }), " Leyendo S.M.A.R.T.…"));
  v.append(page("Salud del disco", "Estado S.M.A.R.T. de cada disco. Si un disco muestra sectores reasignados o pendientes, no lo escanees repetidamente: crea primero una imagen.", box));
  api("smart").then((rs) => box.replaceChildren(...(S.env.smartctl ? [] : [h("div", { class: "notice" }, "Para ver todos los atributos instala smartmontools: brew install smartmontools")]),
    ...rs.map((r) => h("div", { class: "card" }, h("div", { class: "row" }, h("b", {}, r.model || r.disk), h("span", { class: "muted" }, `${r.disk} · ${fmtB(r.size)} · ${r.protocol}${r.ssd ? " · SSD" : ""}`), h("span", { class: "spacer" }),
      h("span", { class: "tag " + { ok: "ok", aviso: "warn", fallo: "bad" }[r.verdict] || "" }, { ok: "Saludable", aviso: "Atención", fallo: "Fallo inminente", desconocido: "Sin datos" }[r.verdict])),
      h("div", { class: "row muted", style: { marginTop: "6px" } }, `macOS: ${r.macos_status}`, r.temperature != null ? ` · ${r.temperature} °C` : "", r.power_on_hours != null ? ` · ${r.power_on_hours} h encendido` : "", ` · fuente: ${r.source}`),
      ...r.warnings.map((w) => h("div", { class: "notice warn", style: { marginTop: "8px" } }, w)),
      r.attributes.length ? h("div", { class: "tbl", style: { marginTop: "10px" } }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "ID"), h("th", {}, "Atributo"), h("th", { class: "num" }, "Valor"), h("th", { class: "num" }, "Peor"), h("th", { class: "num" }, "Umbral"), h("th", { class: "num" }, "Bruto"))),
        h("tbody", {}, ...r.attributes.map((a) => h("tr", {}, h("td", {}, a.id), h("td", {}, a.name), h("td", { class: "num" }, a.value ?? ""), h("td", { class: "num" }, a.worst ?? ""), h("td", { class: "num" }, a.thresh ?? ""), h("td", { class: "num" }, a.raw)))))) : null))))
    .catch((e) => box.replaceChildren(h("div", { class: "notice bad" }, e.message)));
};

VIEWS.history = (v) => {
  const box = h("div", { class: "tbl" }, h("div", { class: "empty" }, h("span", { class: "spin" })));
  const load = () => api("sessions").then((ss) => box.replaceChildren(ss.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Escaneo"), h("th", {}, "Fecha"), h("th", {}, "Origen"), h("th", { class: "num" }, "Archivos"), h("th", {}, "Estado"), h("th", {}))),
    h("tbody", {}, ...ss.map((s) => h("tr", { class: "it" }, h("td", {}, h("b", {}, s.label)), h("td", { class: "muted" }, fmtDate(s.started)), h("td", { class: "mono" }, s.source), h("td", { class: "num" }, s.items),
      h("td", {}, h("span", { class: "tag " + (s.complete ? "ok" : "warn") }, s.complete ? "Completo" : "Incompleto")),
      h("td", {}, h("div", { class: "row" }, h("button", { class: "small", onclick: async () => { const r = await api("sessions/load", { path: s.path }); S.scanFrom = "history"; openScan(r.id); } }, "Abrir"),
        !s.complete ? h("button", { class: "small primary", onclick: async () => { const r = await api("sessions/resume", { path: s.path }); S.scanFrom = "history"; openScan(r.id); } }, "Reanudar") : null,
        h("button", { class: "small ghost danger", onclick: async () => { if (confirm("¿Borrar este escaneo guardado?")) { await api("sessions/delete", { path: s.path }); load(); } } }, "Borrar")))))))
    : h("div", { class: "empty" }, "Aún no hay escaneos guardados. Usa «Guardar escaneo» en la pantalla de resultados.")));
  v.append(page("Historial de escaneos", "Abre resultados guardados sin volver a escanear, o reanuda un escaneo profundo que se detuvo.", box));
  load();
};

VIEWS.mhl = (v) => {
  const out = h("div");
  v.append(page("Verificar MHL", "Comprueba archivos recuperados contra las listas de hash (.mhl) del volcado de rodaje (xxHash64, MD5, SHA1; MHL 1.x y ASC MHL).",
    h("button", { class: "primary", onclick: async () => {
      const dir = await pickPath({ mode: "dir", start: S.env.home });
      if (!dir) return;
      out.replaceChildren(h("span", { class: "spin" }), " Calculando hashes…");
      try {
        const r = await api("mhl", { dir });
        out.replaceChildren(h("p", { class: "muted" }, `${r.mhl.length} archivos MHL`), h("div", { class: "tbl" }, h("table", {}, h("tbody", {}, ...r.rows.map((x) => h("tr", {},
          h("td", {}, h("span", { class: "tag " + (x.status === "OK" ? "ok" : x.status === "no recuperado" ? "warn" : "bad") }, x.status)), h("td", {}, x.file), h("td", { class: "mono muted" }, x.algo || ""), h("td", { class: "muted mono" }, x.recovered || "")))))));
      } catch (e) { out.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
    } }, "Elegir carpeta recuperada…"), h("div", { style: { marginTop: "14px" } }, out)));
};

VIEWS.log = (v) => {
  const pre = h("pre", { class: "card mono", style: { whiteSpace: "pre-wrap", minHeight: "300px" } });
  v.append(page("Registro", "Mensajes del motor.", pre));
  every(2000, async () => { pre.textContent = (await api("log")).join("\n") || "Sin mensajes."; });
};

// ---------------------------------------------------------------- boot

(async function boot() {
  document.getElementById("jobsbtn").onclick = () => { document.getElementById("jobs").classList.toggle("hidden"); refreshJobs(); };
  try {
    S.env = await api("env");
    document.getElementById("ver").textContent = "v" + S.env.version;
    const env = document.getElementById("envbox");
    env.replaceChildren(...[
      S.env.root ? h("div", {}, "● Acceso a discos: sí") : h("div", { class: "warn" }, "● Sin permisos de administrador: solo imágenes y carpetas. Abre RecoFix con su lanzador."),
      S.env.ffmpeg ? null : h("div", { class: "warn" }, "● ffmpeg no instalado: sin vistas previas de vídeo ni reparación (brew install ffmpeg)")].filter(Boolean));
  } catch (e) {
    document.getElementById("view").replaceChildren(page("Sin conexión con el motor", "Abre RecoFix desde su lanzador (el enlace incluye un token de seguridad).",
      h("div", { class: "notice bad" }, e.message)));
    return;
  }
  setInterval(refreshJobs, 2000);
  go("home");
})();
