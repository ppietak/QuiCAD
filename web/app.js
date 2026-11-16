// The page of the editor: the conversation and history on the left, the model on the right.
// Preact + htm, no build step. All state lives in one store; the server pushes
// changes over an event stream, so a reload or a second tab shows the same thing.
//
// A project holds any number of model files and scans; the page works on one
// model at a time (`project.file`), and everything on the left is about it.

import { h, render } from 'preact';
import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'preact/hooks';
import htm from 'htm';

import { LANGUAGES, locale, plural, preferredLanguage, setLanguage, t, translate } from './i18n.js';
import { createViewer, decimalMark, formatMm } from './viewer.js';

const html = htm.bind(h);

const MAX_FILES = 8;
const MAX_FILE_BYTES = 6 * 1024 * 1024;
const START_COMMAND = './start';
// The texts of these two are English, as everywhere; they are translated where
// they are shown (the language is loaded after this module).
const PHASES = {
  starting: 'Starting…',
  thinking: 'Thinking…',
  reading: 'Reading another model…',
  measuring: 'Measuring the STL…',
  looking: 'Looking at the model…',
  editing: 'Changing the model…',
  building: 'Building the model…',
  describing: 'Describing the change…',
  writing: 'Replying…',
};
const SUGGESTIONS = {
  model: [
    'Round the vertical edges with a 2 mm radius',
    'Add four 3.2 mm mounting holes near the corners',
    'Briefly describe what this model is made of',
  ],
  blank: [
    'A 60 × 40 × 5 mm plate with four 4 mm holes in the corners',
    'A wall holder for a 6 mm cable, fixed with one M4 screw',
  ],
  scan: [
    'Recreate this STL as a parametric model',
    'Describe what the object in the STL is made of',
  ],
};
const phaseText = (phase) => t(PHASES[phase] ?? PHASES.thinking);

const isModelFile = (file) => /\.py$/i.test(file.name);
const isScanFile = (file) => /\.stl$/i.test(file.name);
// Model files are shown without their extension: the page only holds models.
const bare = (name) => String(name ?? '').replace(/\.py\b/g, '');

// Day or night: the system's choice unless the person picked one.
function storedTheme() {
  try { return localStorage.getItem('theme'); } catch { return null; }
}
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
function currentTheme() {
  const chosen = storedTheme();
  return chosen === 'light' || chosen === 'dark' ? chosen : darkQuery.matches ? 'dark' : 'light';
}
function applyTheme(chosen) {
  if (chosen) document.documentElement.dataset.theme = chosen;
  else delete document.documentElement.dataset.theme;
  try { chosen ? localStorage.setItem('theme', chosen) : localStorage.removeItem('theme'); } catch { /* not kept */ }
  setState({ theme: currentTheme() });
}
if (storedTheme()) document.documentElement.dataset.theme = storedTheme();

// --- store -------------------------------------------------------------------

let state = {
  boot: 'loading',     // loading | ready | denied | error
  info: null,          // { plan, models, defaultModel, root, engine, limits, projects }
  limits: null,        // subscription usage as last reported: { fiveHour, week }
  projectId: null,
  home: false,         // the dashboard of all projects, over everything else
  project: null,       // the server's snapshot of the open project, with one model open
  opening: null,       // name of the model being switched to
  building: false,
  uploading: 0,        // scans on their way to the server
  peek: null,          // { rev, meta, time } while looking at an older version
  tab: 'chat',         // chat | params | summary | history
  unpicked: false,     // the project was just opened: its list is up and no model is chosen yet
  listing: false,      // the side panel shows the project's models and scans instead of one model
  model: null,         // the Claude model for the next prompt; every session starts on the default
  options: { dims: true, refs: false, scans: false, grid: true, code: false },
  scanOpen: null,      // the STL picked in the list: shown alone, in place of the model
  dialog: null,        // null | { kind: 'duplicate', projectId } | { kind: 'confirm', ... } | { kind: 'camera', sketch }
  spot: null,          // a point on the model picked with the right mouse button, while its prompt is open
  toasts: [],
  dragging: false,
  now: Date.now(),
  theme: 'light',
  lang: 'en',          // the language of the page; the texts themselves live in i18n.js
};
const listeners = new Set();

function setState(patch) {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
  for (const listener of listeners) listener();
}

function useStore() {
  const [, tick] = useReducer((n) => n + 1, 0);
  useEffect(() => {
    listeners.add(tick);
    return () => listeners.delete(tick);
  }, []);
  return state;
}

function patchProject(patch) {
  setState((s) => (s.project ? { project: { ...s.project, ...(typeof patch === 'function' ? patch(s.project) : patch) } } : {}));
}

// --- server ------------------------------------------------------------------

async function reply(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.error ? translate(data.error) : t('The app replied with code {status}.', { status: res.status }));
    error.status = res.status;
    throw error;
  }
  return data;
}

async function api(path, body) {
  return reply(await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
}

const projectPath = (id, file) => `/projects/${id}${file ? `?file=${encodeURIComponent(file)}` : ''}`;
// The address of an operation on the model that is open.
const filePath = (action) => `/projects/${state.projectId}/files/${encodeURIComponent(state.project.file)}/${action}`;
const scanPath = (id, name) => `/api/projects/${id}/scans/${encodeURIComponent(name)}`;

function toast(text, { error = false } = {}) {
  const id = Math.random().toString(36).slice(2);
  setState((s) => ({ toasts: [...s.toasts, { id, text, error }] }));
  setTimeout(() => setState((s) => ({ toasts: s.toasts.filter((item) => item.id !== id) })), error ? 6000 : 3200);
}

// Run an action; a failure becomes a toast instead of an unhandled rejection.
async function attempt(action, done) {
  try {
    await action();
    if (done) toast(done);
    return true;
  } catch (err) {
    toast(err instanceof TypeError ? t('The app is not responding. Check that `{command}` is still running.', { command: START_COMMAND }) : err.message, { error: true });
    return false;
  }
}

function setAddress(id, file) {
  history.replaceState(null, '', `#p=${id}${file && !state.unpicked ? `&f=${encodeURIComponent(file)}` : ''}`);
}

let events = null;
let fileLoad = 0;

/** Open one of the project's models; without a name, the one the server holds current. */
async function openFile(name = null) {
  const id = state.projectId;
  const load = ++fileLoad;
  if (name) setState({ opening: name });
  const project = await api(projectPath(id, name)).catch(() => null);
  if (state.projectId !== id || load !== fileLoad) return;
  if (!project) {
    setState({ opening: null });
    return;
  }
  setState({ project, opening: null, peek: null, building: false });
  setAddress(id, project.file);
}

function onEvent(event) {
  const project = state.project;
  if (!project) return;
  switch (event.type) {
    case 'build':
      if (event.file !== project.file) break;
      if (event.state === 'start') setState({ building: true });
      else {
        setState({ building: false });
        patchProject({ build: event.build, good: event.good });
      }
      break;
    case 'files':
      patchProject({ files: event.files, scans: event.scans });
      // The open model is gone (renamed, removed), or the first one just appeared.
      if (project.file ? !event.files.some((file) => file.name === project.file) : event.files.length > 0) openFile();
      break;
    case 'turn':
      patchProject({ turn: event.turn });
      break;
    case 'turn.phase':
      patchProject((p) => (p.turn ? { turn: { ...p.turn, phase: event.phase } } : {}));
      break;
    case 'turn.tokens':
      patchProject((p) => (p.turn ? { turn: { ...p.turn, tokens: event.tokens } } : {}));
      break;
    case 'turn.text':
      patchProject((p) => (p.turn ? { turn: { ...p.turn, reply: event.reset ? '' : p.turn.reply + (event.delta ?? '') } } : {}));
      break;
    case 'turn.done':
    case 'journal': {
      const about = event.entry.file ?? null;
      patchProject((p) => ({
        ...(about !== null && about === p.file ? { journal: [...p.journal, event.entry] } : {}),
        ...(about !== null && about === p.file ? { head: event.head, history: event.history, undoTo: event.undoTo, redoTo: event.redoTo } : {}),
        ...(event.type === 'turn.done' ? { turn: null } : {}),
      }));
      if (event.entry.limits) setState({ limits: event.entry.limits });
      break;
    }
    default:
      break;
  }
}

function connectEvents(id) {
  events?.close();
  let opened = false;
  events = new EventSource(`/api/projects/${id}/events`);
  events.onmessage = (message) => onEvent(JSON.parse(message.data));
  events.onopen = () => {
    // After a dropped connection, events were missed: take a fresh snapshot.
    if (opened && state.projectId === id) openFile(state.project?.file ?? null);
    opened = true;
  };
}

async function openProject(id, file = null) {
  events?.close();
  fileLoad += 1;
  setState({ home: false, projectId: id, project: null, opening: null, peek: null, building: false, tab: 'chat', listing: false, unpicked: !file, scanOpen: null, spot: null });
  setAddress(id, file);
  const ok = await attempt(async () => {
    const project = await api(projectPath(id, file));
    if (state.projectId !== id) return;
    const seen = project.journal.findLast((entry) => entry.limits)?.limits;
    setState({ project, ...(seen && seen.at > (state.limits?.at ?? 0) ? { limits: seen } : {}) });
    setAddress(id, project.file);
    connectEvents(id);
  });
  // Nothing to show on the page without a project: back to the list of them.
  if (!ok && state.projectId === id) setState({ projectId: null, home: true });
}

async function refreshProjects() {
  const info = await api('/state').catch(() => null);
  if (info) setState({ info });
}

async function uploadScan(projectId, file) {
  return reply(await fetch(scanPath(projectId, file.name), { method: 'PUT', body: file }));
}

/** Add meshes to the open project as scans to reverse engineer. Resolves to the names they got. */
async function addScans(files) {
  const id = state.projectId;
  const names = [];
  if (!id || !files.length) return names;
  setState((s) => ({ uploading: s.uploading + files.length }));
  for (const file of files) {
    await attempt(async () => {
      names.push((await uploadScan(id, file)).name);
      toast(t('STL added'));
    });
    setState((s) => ({ uploading: s.uploading - 1 }));
  }
  return names;
}

/** Show one of the project's models with its conversation. */
async function showModel(name) {
  setState({ unpicked: false });
  const other = name !== state.project?.file;
  if (other) await openFile(name);
  setState({ listing: false, scanOpen: null, ...(other ? { tab: 'chat' } : {}) }); // another model opens on its conversation
  setAddress(state.projectId, name);
}

/** Back to the list of the project's models; the address then names only the project. */
function showList() {
  setState({ listing: true, scanOpen: null, peek: null, spot: null });
  history.replaceState(null, '', `#p=${state.projectId}`);
}

/** Show one of the project's STL files, alone. */
function showScan(name) {
  setState({ scanOpen: name, listing: false, peek: null, spot: null });
}

/** A new, empty model with a generic name, opened at once; it is renamed in the list. */
async function newModel() {
  const { file } = await api(`/projects/${state.projectId}/files`, { name: t('model'), unique: true });
  await showModel(file);
}

/** A new model named after an STL, and the prompt that recreates the STL in it, sent at once. */
async function modelFromScan(scan) {
  const id = state.projectId;
  const { file } = await api(`/projects/${id}/files`, { name: scan.replace(/\.stl$/i, ''), unique: true });
  await showModel(file);
  setState({ tab: 'chat' });
  await api(`/projects/${id}/prompt`, { file, text: t('Recreate @{name} as a parametric model', { name: scan }), model: state.model });
}

const modelThumb = (project, file) => `/api/projects/${project.id}/files/${encodeURIComponent(file.name)}/thumb?v=3-${file.name === project.file && project.build?.key ? project.build.key : Math.round(file.updatedAt)}`;
const scanThumb = (project, scan) => `${scanPath(project.id, scan.name)}/thumb?v=3-${Math.round(scan.updatedAt)}`;

/** Bring model files (.py) into the open project. */
async function importModels(files) {
  for (const item of files) {
    await attempt(async () => {
      await api(`/projects/${state.projectId}/files`, { name: item.name, source: await item.text() });
      toast(t('Model added'));
    });
  }
}

/** Ask for STL files and add them to the open project. */
function chooseScans() {
  const input = Object.assign(document.createElement('input'), { type: 'file', accept: '.stl', multiple: true });
  input.addEventListener('change', () => addScans([...input.files]));
  input.click();
}

/**
 * A new project, created at once: empty, or with the dropped files in it
 * (.py as models, .stl as scans). Its name can be changed later.
 */
async function createProject(files = []) {
  const models = files.filter(isModelFile);
  const scans = files.filter(isScanFile);
  const named = files.map(nameFrom).find(Boolean);
  const { id } = await api('/projects', {
    name: named || 'new project',
    files: await Promise.all(models.map(async (file) => ({ name: file.name, source: await file.text() }))),
  });
  // The project exists from here on: a scan that fails to upload is reported, not fatal.
  for (const scan of scans) await attempt(() => uploadScan(id, scan));
  await refreshProjects();
  await openProject(id);
}

async function boot() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get('t');
  if (token) {
    // Trade the key from the address for a cookie, then drop it from the address.
    await fetch('/api/session', { method: 'POST', headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
    history.replaceState(null, '', location.pathname);
  }
  let info;
  try {
    info = await api('/state');
  } catch (err) {
    setState({ boot: err.status === 401 ? 'denied' : 'error' });
    return;
  }
  // A project is open only when the address names one; otherwise the list of them.
  const first = info.projects.find((p) => p.id === params.get('p'));
  setState({ boot: 'ready', info, limits: info.limits ?? null, model: info.defaultModel });
  if (first) openProject(first.id, params.get('f'));
  else setState({ home: true });
}

async function peekVersion(entry) {
  const { project } = state;
  if (!project || entry.hash === project.head) {
    setState({ peek: null });
    return;
  }
  setState({ peek: { rev: entry.hash, time: entry.time, meta: null } });
  await attempt(async () => {
    const meta = await api(filePath(`version/${entry.hash}`));
    if (state.peek?.rev === entry.hash) setState({ peek: { rev: entry.hash, time: entry.time, meta } });
  });
}

async function restoreVersion(rev) {
  const ok = await attempt(() => api(filePath('restore'), { rev }), t('Restored'));
  if (ok) setState({ peek: null });
}

// Whether an STL can be drawn with the model: only the ones the model reproduces,
// and only while that is switched on in the view. Any STL is seen by picking it
// in the list, where it is shown alone.
function scanVisible(name, meta) {
  return (meta?.scans ?? []).some((scan) => scan.name === name);
}

// --- formatting --------------------------------------------------------------

function pad(n) {
  return String(n).padStart(2, '0');
}

function when(time, now) {
  const date = new Date(time);
  const seconds = (now - time) / 1000;
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (seconds < 50) return t('just now');
  if (seconds < 3300) return t('{count} min ago', { count: Math.max(1, Math.round(seconds / 60)) });
  const today = new Date(now);
  if (date.toDateString() === today.toDateString()) return hm;
  const yesterday = new Date(now - 86_400_000);
  if (date.toDateString() === yesterday.toDateString()) return t('yesterday {time}', { time: hm });
  return `${date.toLocaleDateString(locale(), { day: 'numeric', month: 'short' })} ${hm}`;
}

// A point in time named exactly: "21:14" today, "13 Nov, 21:14" otherwise.
function stamp(time, now) {
  const date = new Date(time);
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (date.toDateString() === new Date(now).toDateString()) return hm;
  return `${date.toLocaleDateString(locale(), { day: 'numeric', month: 'short' })}, ${hm}`;
}

function exact(time) {
  return new Date(time).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'medium' });
}

// A number written by the code ("1.5"), with the decimal separator of the language.
function localized(value) {
  return String(value).replace('.', decimalMark());
}

function seconds(ms) {
  return `${localized((ms / 1000).toFixed(1))} s`;
}

// A count of tokens in a few characters: 840, 6.1k, 17k.
function compact(total) {
  if (total < 1000) return String(total);
  return `${localized((total / 1000).toFixed(total < 10_000 ? 1 : 0))}k`;
}

function number(value, digits = 2) {
  return localized(Number(value.toFixed(digits)));
}

function fileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${number(bytes / 1024 / 1024, 1)} MB`;
}

function capitalized(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// What a parameter is called on the page: its comment in the file, else its
// variable name made readable. The variable itself stays out of sight.
function paramLabel(name, comment) {
  return capitalized((comment || String(name ?? '').replace(/_+/g, ' ').toLowerCase()).trim());
}

// A text of the interface with elements in its {placeholders}: the pieces in order.
function tx(text, slots) {
  return t(text).split(/\{(\w+)\}/).map((piece, index) => (index % 2 ? slots[piece] : piece));
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// Just enough Markdown for an answer to read well: `code` and **bold**.
function rich(text) {
  return escapeHtml(text)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, type: file.type, size: file.size, data: String(reader.result).split(',')[1] ?? '' });
    reader.onerror = () => reject(new Error(t('Could not read the file {name}.', { name: file.name })));
    reader.readAsDataURL(file);
  });
}

// --- small pieces ------------------------------------------------------------

const ICONS = {
  project: '<path d="m12.99 6.74 1.93 3.44"/><path d="M19.136 12a10 10 0 0 1-14.271 0"/><path d="m21 21-2.16-3.84"/><path d="m3 21 8.02-14.26"/><circle cx="12" cy="5" r="2"/>',
  box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  viewIso: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/>',
  viewTop: '<path d="M12 3l8 4.5-8 4.5-8-4.5z" fill="currentColor"/><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/>',
  viewFront: '<path d="M4 7.5l8 4.5v9l-8-4.5z" fill="currentColor"/><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/>',
  viewSide: '<path d="M20 7.5l-8 4.5v9l8-4.5z" fill="currentColor"/><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/>',
  sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  clip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  camera: '<path d="M14.5 3h-5L7 6H4a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13.5" r="3.5"/>',
  picture: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
  up: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  stop: '<rect width="12" height="12" x="6" y="6" rx="2" fill="currentColor"/>',
  ruler: '<path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.41 2.41 0 0 1 0-3.4l2.6-2.6a2.41 2.41 0 0 1 3.4 0Z"/><path d="m14.5 12.5 2-2"/><path d="m11.5 9.5 2-2"/><path d="m8.5 6.5 2-2"/><path d="m17.5 15.5 2-2"/>',
  grid: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/>',
  layers: '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
  fit: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
  scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
  sliders: '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
  code: '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  eye: '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M10.73 5.08a10.74 10.74 0 0 1 11.2 6.57 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-1.44 2.49"/><path d="M14.08 14.16a3 3 0 0 1-4.24-4.24"/><path d="M17.48 17.5a10.75 10.75 0 0 1-15.42-5.15 1 1 0 0 1 0-.7 10.75 10.75 0 0 1 4.45-5.14"/><path d="m2 2 20 20"/>',
  pencil: '<path d="M21.17 6.81a1 1 0 0 0-3.98-3.98L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.36-1.32a2 2 0 0 0 .83-.5z"/><path d="m15 5 4 4"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  pin: '<path d="M20 10c0 4.99-5.54 10.19-7.4 11.8a1 1 0 0 1-1.2 0C9.54 20.19 4 14.99 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
};

function Icon({ name }) {
  return html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true" dangerouslySetInnerHTML=${{ __html: ICONS[name] }} />`;
}

function IconButton({ icon, label, onClick, on, disabled, small = true }) {
  return html`<button type="button" class=${`btn btn-ghost btn-icon ${small ? 'btn-sm' : ''} ${on ? 'is-on' : ''}`}
    title=${label} aria-label=${label} aria-pressed=${on == null ? undefined : String(!!on)} disabled=${disabled} onClick=${onClick}>
    <${Icon} name=${icon} />
  </button>`;
}

// A small picture of a model or an STL beside its name, on the same ground as the 3D view;
// where there is no shape yet (an empty model), only the ground.
function Thumb({ src, scan = false }) {
  const [failed, setFailed] = useState(null);
  return html`<span class=${`thumb ${scan ? 'is-scan' : ''}`}>${src && failed !== src && html`<img src=${src} alt="" loading="lazy" onError=${() => setFailed(src)} />`}</span>`;
}

function ThemeToggle({ theme, small = false }) {
  const dark = theme === 'dark';
  return html`<${IconButton} icon=${dark ? 'sun' : 'moon'} label=${dark ? t('Light theme') : t('Dark theme')} small=${small}
    onClick=${() => applyTheme(dark ? 'light' : 'dark')} />`;
}

// The language of the page, chosen on the dashboard and remembered.
function LanguagePicker({ lang }) {
  const choose = async (event) => setState({ lang: await setLanguage(event.target.value, { remember: true }) });
  return html`<select class="select" aria-label=${t('Language')} title=${t('Language')} value=${lang} onChange=${choose}>
    ${LANGUAGES.map((item) => html`<option value=${item.code} key=${item.code}>${item.label}</option>`)}
  </select>`;
}

// Closes a menu or popover on a click elsewhere or on Escape.
function useDismiss(open, close) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (event) => { if (ref.current && !ref.current.contains(event.target)) close(); };
    const onKey = (event) => { if (event.key === 'Escape') close(); };
    // Pointer events, caught on the way down: the 3D view swallows mouse events.
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return ref;
}

// --- dialogs -----------------------------------------------------------------

const closeDialog = () => setState({ dialog: null });

function Dialog({ title, lead, children, onClose = closeDialog }) {
  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
  return html`<div class="overlay" onMouseDown=${(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div class="dialog" role="dialog" aria-modal="true" aria-label=${title}>
      <h2>${title}</h2>
      ${lead && html`<p>${lead}</p>`}
      ${children}
    </div>
  </div>`;
}

// One form in a dialog: runs `action`, shows what went wrong, closes on success.
function useSubmit(action) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const submit = async (event) => {
    event?.preventDefault();
    if (busy) return;
    setBusy(true);
    setProblem('');
    try {
      await action();
      closeDialog();
    } catch (err) {
      setProblem(err.message);
      setBusy(false);
    }
  };
  return { busy, problem, setProblem, submit };
}

// "assembly.py" says nothing about what the model is; other names do.
function nameFrom(file) {
  const base = file.name.replace(/\.(py|stl)$/i, '');
  return /^(assembly|model|main|part|test)$/i.test(base) ? '' : base;
}

function DuplicateDialog({ projectId }) {
  const [name, setName] = useState(`${projectId}-copy`);
  const field = useRef(null);
  useEffect(() => field.current?.select(), []);
  const form = useSubmit(async () => {
    const { id } = await api(`/projects/${projectId}/duplicate`, { name: name.trim() });
    await refreshProjects();
    await openProject(id);
    toast(t('Duplicated'));
  });
  return html`<${Dialog} title=${t('Duplicate project')} lead=${t('The copy gets all the models, conversations and history of the project {name}. From then on the two are independent.', { name: projectId })}>
    <form onSubmit=${form.submit}>
      <label for="copy-name">${t('Name of the copy')}</label>
      <input id="copy-name" ref=${field} class="input" value=${name} onInput=${(e) => setName(e.target.value)} />
      ${form.problem && html`<div class="problem">${form.problem}</div>`}
      <div class="dialog-actions">
        <button type="button" class="btn" onClick=${closeDialog}>${t('Cancel')}</button>
        <button type="submit" class="btn btn-primary" disabled=${!name.trim() || form.busy}>${form.busy ? t('Copying…') : t('Create copy')}</button>
      </div>
    </form>
  <//>`;
}

/** Another name for a project; its folder is renamed with it. */
async function renameProject(projectId, name) {
  const { id } = await api(`/projects/${projectId}/rename`, { name });
  await refreshProjects();
  if (projectId === state.projectId && id !== projectId) await openProject(id, state.unpicked ? null : state.project?.file ?? null);
}

/** Another name for a model or an STL of the open project. */
async function renameFile(file, name, scan = false) {
  const id = state.projectId;
  if (scan) {
    const renamed = await fetch(`${scanPath(id, file)}/rename`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to: name }) }).then(reply);
    if (state.scanOpen === file) setState({ scanOpen: renamed.name });
    return;
  }
  const renamed = await api(`/projects/${id}/files/${encodeURIComponent(file)}/rename`, { to: name });
  if (file === state.project?.file) await openFile(renamed.file);
}

// A name edited where it stands. Enter or a click elsewhere keeps the new name,
// Escape the old one. A name that is refused stays open to be corrected.
function NameEdit({ value, hint, onSave, onClose }) {
  const [text, setText] = useState(value);
  const [busy, setBusy] = useState(false);
  const field = useRef(null);
  const closed = useRef(false);
  // At once, before anything is drawn: what is typed next goes into the field.
  useLayoutEffect(() => {
    field.current?.focus();
    field.current?.select();
  }, []);
  const finish = async (keep, left = false) => {
    if (closed.current) return;
    const name = text.trim();
    closed.current = true;
    if (!keep || !name || name === value) return onClose();
    setBusy(true);
    if (await attempt(() => onSave(name)) || left) return onClose();
    closed.current = false;
    setBusy(false);
    field.current?.focus();
    return undefined;
  };
  return html`<input ref=${field} class="input name-edit" value=${text} aria-label=${t('New name')} title=${hint} readOnly=${busy} spellcheck=${false}
    onInput=${(event) => setText(event.target.value)} onClick=${(event) => event.stopPropagation()} onBlur=${() => finish(true, true)}
    onKeyDown=${(event) => {
      if (event.key !== 'Enter' && event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      finish(event.key === 'Enter');
    }} />`;
}

function ConfirmDialog({ title, lead, label, action }) {
  const form = useSubmit(action);
  return html`<${Dialog} title=${title} lead=${lead}>
    ${form.problem && html`<div class="problem">${form.problem}</div>`}
    <div class="dialog-actions">
      <button type="button" class="btn" onClick=${closeDialog}>${t('Cancel')}</button>
      <button type="button" class="btn btn-danger" disabled=${form.busy} onClick=${form.submit}>${label}</button>
    </div>
  <//>`;
}

// --- camera ------------------------------------------------------------------

// A photo taken here goes with the next prompt like any other attachment: a
// sketch on paper, the part something has to fit, a drawing.
const CAN_SHOOT = !!navigator.mediaDevices?.getUserMedia;
const PHOTO_EDGE = 2048;       // the longer side of a photo, at most
const CAMERA_KEY = 'camera';   // the camera picked last time
const FOCUS_KEY = 'cameraFocus'; // per camera: where its lens is held, when not left to the camera
const ASPECT_KEY = 'cameraAspect'; // the shape of the picture last time: the window opens in it
const PHOTO_SIZE = { width: { ideal: 1920 }, height: { ideal: 1080 } };

function kept(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function keep(key, value) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* not kept */ }
}

function cameraProblem(err) {
  switch (err?.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return t('The camera is blocked. Allow it for this page (the icon in the address bar), then try again.');
    case 'NotFoundError':
    case 'OverconstrainedError':
      return t('No camera was found.');
    case 'NotReadableError':
    case 'AbortError':
      return t('The camera is in use by another program.');
    default:
      return t('The camera could not be started.');
  }
}

// What a camera lets the page do about its focus: `auto` is the mode in which
// it focuses by itself, `range` the distances its lens can be held at. Few
// cameras tell, and only to some browsers; the others focus as they please.
function focusOf(track) {
  const can = track.getCapabilities?.() ?? {};
  const modes = can.focusMode ?? [];
  const range = can.focusDistance;
  return {
    auto: ['continuous', 'single-shot'].find((mode) => modes.includes(mode)) ?? null,
    range: modes.includes('manual') && range && range.max > range.min ? range : null,
  };
}

// The lens held at `distance`, or left to the camera again (null).
function focusAt(track, auto, distance) {
  const how = distance == null ? { focusMode: auto } : { focusMode: 'manual', focusDistance: distance };
  return track.applyConstraints({ ...PHOTO_SIZE, advanced: [how] }).catch(() => {});
}

// "FaceTime HD Camera (3A71:F4B5)" is the camera's name and its USB id.
const cameraName = (device, index) => device.label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)$/i, '') || t('Camera {number}', { number: index + 1 });

const CROP = { x: 0.1, y: 0.1, w: 0.8, h: 0.8 }; // the frame a new photo gets: its middle, as parts of its sides
const WHOLE = { x: 0, y: 0, w: 1, h: 1 };
const CROP_MIN = 0.08;                            // the shortest side a frame can have

// The frame a new photo gets, where the picture is shown: its middle — and, in a
// small window, no further up or down than the buttons that lie on the picture.
function middleFrame(frame) {
  const box = frame?.querySelector('.camera-photo')?.getBoundingClientRect();
  const top = frame?.querySelector('.camera-top')?.getBoundingClientRect();
  const bar = frame?.querySelector('.camera-bar')?.getBoundingClientRect();
  if (!box?.height || !top || !bar) return CROP;
  const from = Math.min(0.3, Math.max(CROP.y, (top.bottom - box.top) / box.height));
  const to = Math.max(0.7, Math.min(CROP.y + CROP.h, (bar.top - box.top) / box.height));
  return { ...CROP, y: from, h: to - from };
}

// The frame of a photo: the part of it that is kept. Its corners and sides are
// dragged to size it, its inside to move it; a drag outside it draws a new one,
// and a double click takes the whole photo (or the middle again).
function CropBox({ value, onChange, onHold }) {
  const area = useRef(null);
  const middle = () => middleFrame(area.current?.closest('.camera-frame'));
  const drag = useRef(null);
  const part = (event, rect) => ({ x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height });
  const within = (n, low, high) => Math.min(high, Math.max(low, n));
  // From where the drag began to where the pointer is, inside the photo and never too short.
  const span = (anchor, to) => {
    const a = within(anchor, 0, 1);
    const b = within(to, 0, 1);
    let low = Math.min(a, b);
    let high = Math.max(a, b);
    if (high - low < CROP_MIN) {
      if (b >= a) { high = Math.min(1, low + CROP_MIN); low = high - CROP_MIN; }
      else { low = Math.max(0, high - CROP_MIN); high = low + CROP_MIN; }
    }
    return [low, high];
  };
  const start = (mode) => (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = area.current.getBoundingClientRect();
    drag.current = { mode, rect, from: part(event, rect), was: value, drawn: false };
    area.current.setPointerCapture(event.pointerId);
    area.current.classList.add('is-held'); // at once: the frame follows the pointer without easing
    onHold(true);
  };
  const move = (event) => {
    const held = drag.current;
    if (!held) return;
    const { mode, rect, from, was } = held;
    const at = part(event, rect);
    let left = was.x;
    let top = was.y;
    let right = was.x + was.w;
    let bottom = was.y + was.h;
    if (mode === 'move') {
      left = within(was.x + at.x - from.x, 0, 1 - was.w);
      top = within(was.y + at.y - from.y, 0, 1 - was.h);
      right = left + was.w;
      bottom = top + was.h;
    } else if (mode === 'draw') {
      // A press alone changes nothing: the new frame begins once the pointer has travelled a little.
      if (!held.drawn && Math.hypot((at.x - from.x) * rect.width, (at.y - from.y) * rect.height) < 4) return;
      held.drawn = true;
      [left, right] = span(from.x, at.x);
      [top, bottom] = span(from.y, at.y);
    } else {
      // A side follows the pointer from where it was taken hold of, and stops short of the opposite one.
      if (mode.includes('w')) left = within(was.x + at.x - from.x, 0, right - CROP_MIN);
      if (mode.includes('e')) right = within(was.x + was.w + at.x - from.x, left + CROP_MIN, 1);
      if (mode.includes('n')) top = within(was.y + at.y - from.y, 0, bottom - CROP_MIN);
      if (mode.includes('s')) bottom = within(was.y + was.h + at.y - from.y, top + CROP_MIN, 1);
    }
    onChange({ x: left, y: top, w: right - left, h: bottom - top });
  };
  const end = () => {
    if (!drag.current) return;
    drag.current = null;
    area.current?.classList.remove('is-held');
    onHold(false);
  };
  // Taken away in the middle of a drag (the picture was taken with a key): the buttons come back.
  useEffect(() => () => onHold(false), []);
  const whole = value.w > 0.999 && value.h > 0.999;
  const percent = (n) => `${(n * 100).toFixed(3)}%`;
  // The pointer is held by the whole area, so that is where a double click arrives. A press
  // on the picture leaves the focus where it was: on the button that Space or Enter presses.
  return html`<div class="crop" ref=${area} onPointerDown=${start('draw')} onPointerMove=${move} onPointerUp=${end} onPointerCancel=${end} onLostPointerCapture=${end}
    onMouseDown=${(event) => event.preventDefault()} onDblClick=${() => onChange(whole ? middle() : WHOLE)}>
    <div class="crop-box" style=${{ left: percent(value.x), top: percent(value.y), width: percent(value.w), height: percent(value.h) }}
      title=${t('Drag to move the frame. Double click: the whole photo')} onPointerDown=${start('move')}>
      ${['n', 'e', 's', 'w', 'nw', 'ne', 'se', 'sw'].map((side) => html`<span class=${`crop-handle is-${side}`} key=${side} onPointerDown=${start(side)} />`)}
    </div>
  </div>`;
}

// The camera, live; one press takes the picture, a second one uses it. Live, the
// picture is a mirror, so that a sheet held up to the camera moves the way the
// hand does; the picture that is taken is the real one, with its text readable.
// A frame lies on the picture, before it is taken and after: what is inside it
// is kept. Everything to press lies on the picture itself.
function CameraDialog({ onUse }) {
  const video = useRef(null);
  const still = useRef(null);   // the picture that was taken, as the camera gave it
  const shutter = useRef(null);
  const accept = useRef(null);
  const [phase, setPhase] = useState('starting'); // starting | live | shot | failed
  const [problem, setProblem] = useState('');
  const [round, again] = useReducer((n) => n + 1, 0);
  const [wanted, setWanted] = useState(() => kept(CAMERA_KEY));
  const [cameras, setCameras] = useState([]);
  const [active, setActive] = useState('');
  // The window has the shape of the picture from the start, so it does not change size when
  // the camera comes on: the shape seen last time, or the one most cameras have.
  const [aspect, setAspect] = useState(() => {
    const last = Number.parseFloat(kept(ASPECT_KEY));
    return last > 0.3 && last < 4 ? last : 16 / 9;
  });
  const [mirror, setMirror] = useState(true); // not for a camera that looks away from the person
  const shotAt = useRef(0);
  const [photo, setPhoto] = useState(16 / 9); // the shape of the picture taken
  const [crop, setCrop] = useState(WHOLE);    // the part of the picture that is kept, in the picture's own terms
  const [held, setHeld] = useState(false);    // while the frame is being dragged, the buttons step aside
  const framed = useRef(false);
  const lens = useRef(null);    // the camera in use, for its focus: { track, id, auto, next, busy }
  const [focus, setFocus] = useState(null); // null: not ours to set | { auto, range, manual, distance }

  useEffect(() => {
    let media = null;
    let gone = false;
    const list = async () => {
      const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
      if (!gone) setCameras(devices.filter((device) => device.kind === 'videoinput' && device.deviceId));
    };
    const fail = (err) => {
      setProblem(cameraProblem(err));
      setPhase((was) => (was === 'shot' ? was : 'failed')); // a picture already taken stays
    };
    setPhase('starting');
    setActive('');
    setFocus(null);
    navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { ...PHOTO_SIZE, ...(wanted ? { deviceId: { exact: wanted } } : { facingMode: 'environment' }) },
    }).then(async (stream) => {
      media = stream;
      if (gone) return stream.getTracks().forEach((track) => track.stop());
      const [track] = stream.getVideoTracks();
      track?.addEventListener('ended', () => { if (!gone) fail(); }); // unplugged
      video.current.srcObject = stream;
      await video.current.play().catch(() => {});
      if (gone) return undefined;
      const id = track?.getSettings().deviceId ?? '';
      setActive(id);
      setMirror(track?.getSettings().facingMode !== 'environment');
      setPhase('live');
      // Focus: by itself where the camera can, unless its lens was set by hand the last time.
      const { auto, range } = track ? focusOf(track) : {};
      if (auto || range) {
        const held = range ? Number.parseFloat(kept(`${FOCUS_KEY}:${id}`)) : NaN;
        const manual = Number.isFinite(held) || !auto;
        const distance = Number.isFinite(held) ? Math.min(range.max, Math.max(range.min, held)) : track.getSettings().focusDistance ?? range?.min;
        lens.current = { track, id, auto };
        if (range) setFocus({ auto, range, manual, distance });
        if (auto || Number.isFinite(held)) focusAt(track, auto, Number.isFinite(held) ? distance : null);
      }
      return list();
    }).catch((err) => {
      if (gone) return;
      // The camera picked last time is not there any more: whichever there is, then.
      if (wanted && (err?.name === 'OverconstrainedError' || err?.name === 'NotFoundError')) {
        keep(CAMERA_KEY, null);
        setWanted(null);
        return;
      }
      fail(err);
      list();
    });
    return () => {
      gone = true;
      lens.current = null;
      media?.getTracks().forEach((track) => track.stop());
      if (video.current) video.current.srcObject = null;
    };
  }, [wanted, round]);

  // The key that comes next is always under the finger: Space takes, Enter uses.
  useEffect(() => { (phase === 'shot' ? accept : shutter).current?.focus(); }, [phase]);
  // When the picture first comes on, the frame closes in from its edges: that shows it is there, and can be moved.
  useEffect(() => {
    if (phase !== 'live' || framed.current) return;
    framed.current = true;
    requestAnimationFrame(() => requestAnimationFrame(() => setCrop(middleFrame(video.current?.closest('.camera-frame')))));
  }, [phase]);

  const shoot = () => {
    const node = video.current;
    if (phase !== 'live' || !node?.videoWidth) return;
    const scale = Math.min(1, PHOTO_EDGE / Math.max(node.videoWidth, node.videoHeight));
    const canvas = still.current;
    canvas.width = Math.round(node.videoWidth * scale);
    canvas.height = Math.round(node.videoHeight * scale);
    canvas.getContext('2d').drawImage(node, 0, 0, canvas.width, canvas.height);
    shotAt.current = performance.now();
    setPhoto(canvas.width / canvas.height);
    setPhase('shot');
  };
  // The second click of a double click on the shutter lands on what replaces it: not meant.
  const hasty = (event) => event?.detail > 0 && performance.now() - shotAt.current < 400;
  const use = (event) => {
    const taken = still.current;
    if (phase !== 'shot' || !taken) return;
    if (hasty(event)) return;
    // What is inside the frame, in the picture's own pixels.
    const x = Math.round(crop.x * taken.width);
    const y = Math.round(crop.y * taken.height);
    const w = Math.max(1, Math.min(taken.width - x, Math.round(crop.w * taken.width)));
    const h = Math.max(1, Math.min(taken.height - y, Math.round(crop.h * taken.height)));
    const out = Object.assign(document.createElement('canvas'), { width: w, height: h });
    out.getContext('2d').drawImage(taken, x, y, w, h, 0, 0, w, h);
    const data = out.toDataURL('image/jpeg', 0.9).split(',')[1] ?? '';
    closeDialog();
    onUse({ type: 'image/jpeg', size: Math.floor(data.length * 3 / 4), data });
  };
  const retake = (event) => {
    if (hasty(event)) return;
    // Back to the live picture; a camera that went away meanwhile is started again.
    if (video.current?.srcObject?.getVideoTracks()[0]?.readyState === 'live') setPhase('live');
    else again();
  };
  // The lens to a distance picked on the slider, or back to the camera (null). A slider
  // says a lot at once; the camera is told one thing at a time, the latest.
  const focusTo = (distance) => {
    const job = lens.current;
    if (!job) return;
    setFocus((was) => was && { ...was, manual: distance != null, distance: distance ?? was.distance });
    keep(`${FOCUS_KEY}:${job.id}`, distance == null ? null : String(distance));
    job.next = { distance };
    if (job.busy) return;
    job.busy = true;
    (async () => {
      while (job.next) {
        const { distance: to } = job.next;
        job.next = null;
        await focusAt(job.track, job.auto, to);
      }
      job.busy = false;
    })();
  };
  // Automatic focus on and off; off holds the lens where the camera had it.
  const autofocus = () => focusTo(focus.manual ? null : lens.current?.track.getSettings().focusDistance ?? focus.distance);
  const pick = (id) => {
    keep(CAMERA_KEY, id);
    setWanted(id);
  };

  const keys = useRef(null);
  keys.current = (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === 'Escape') return closeDialog();
    // Space and Enter belong to the button that has the focus; with none, to the step that comes next.
    if ((event.key === ' ' || event.key === 'Enter') && !event.target.closest?.('button, select')) {
      event.preventDefault();
      if (phase === 'shot') use();
      else shoot();
    }
  };
  useEffect(() => {
    const onKey = (event) => keys.current(event);
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const size = (event) => {
    const { videoWidth, videoHeight } = event.target;
    if (!videoWidth || !videoHeight) return;
    setAspect(videoWidth / videoHeight);
    keep(ASPECT_KEY, (videoWidth / videoHeight).toFixed(4));
  };
  const chosen = active || wanted || ''; // in the list: the camera in use, or the one being started
  // The live picture is a mirror, the frame is kept in the terms of the real one: left and right swap on the way.
  const mirrored = mirror && phase !== 'shot';
  const swap = (frame) => (mirrored ? { ...frame, x: 1 - frame.x - frame.w } : frame);

  return html`<div class="overlay" onMouseDown=${(event) => { if (event.target === event.currentTarget) closeDialog(); }}>
    <div class="dialog camera" role="dialog" aria-modal="true" aria-label=${t('Take a photo')} style=${`--aspect:${aspect}`}>
      <div class=${`camera-frame ${held ? 'is-cropping' : ''}`}>
        <video ref=${video} class=${`${phase === 'live' || phase === 'shot' ? '' : 'is-off'} ${mirror ? 'is-mirror' : ''}`} playsInline muted autoPlay
          onLoadedMetadata=${size} onResize=${size} />
        <div class="camera-photo" style=${`--photo:${phase === 'shot' ? photo : aspect}`}>
          <canvas ref=${still} class=${phase === 'shot' ? '' : 'is-off'} />
          ${(phase === 'live' || phase === 'shot') && html`<${CropBox} key=${phase} value=${swap(crop)} onChange=${(frame) => setCrop(swap(frame))} onHold=${setHeld} />`}
        </div>
        ${phase === 'shot' && html`<span class="camera-flash" />`}
        ${phase === 'starting' && html`<div class="camera-note" role="status"><span class="spinner" />${t('Starting the camera…')}</div>`}
        ${phase === 'failed' && html`<div class="camera-note" role="alert">
          <p>${problem}</p>
          <button type="button" class="btn" onClick=${again}>${t('Try again')}</button>
        </div>`}
        <div class="camera-top">
          ${cameras.length > 1 && phase !== 'shot' && html`<select class="select" aria-label=${t('Camera')} title=${t('Camera')} value=${chosen}
            onChange=${(event) => pick(event.target.value)}>
            ${!cameras.some((device) => device.deviceId === chosen) && html`<option value=${chosen} key="none" disabled hidden>${t('Camera')}</option>`}
            ${cameras.map((device, index) => html`<option value=${device.deviceId} key=${device.deviceId}>${cameraName(device, index)}</option>`)}
          </select>`}
          <span class="grow" />
          <button type="button" class="camera-close" title=${t('Close')} aria-label=${t('Close')} onClick=${closeDialog}><${Icon} name="x" /></button>
        </div>
        ${phase === 'shot' && html`<div class="camera-bar">
          <div class="camera-actions">
            <button type="button" class="btn" onClick=${retake}>${t('Retake')}</button>
            <button type="button" class="btn btn-primary" ref=${accept} onClick=${use}><${Icon} name="check" />${t('Use photo')}</button>
          </div>
        </div>`}
        ${(phase === 'live' || phase === 'starting') && html`<div class="camera-bar">
          <button type="button" class="btn btn-primary shutter" ref=${shutter} title=${t('Take the photo (Space)')}
            disabled=${phase !== 'live'} onClick=${shoot}><${Icon} name="camera" />${t('Take the photo')}</button>
          ${focus && phase === 'live' && html`<div class="camera-focus">
            <span id="camera-focus">${t('Focus')}</span>
            <input type="range" aria-labelledby="camera-focus" class=${focus.manual ? '' : 'is-auto'} min=${focus.range.min} max=${focus.range.max}
              step=${focus.range.step || 'any'} value=${focus.distance} onInput=${(event) => focusTo(Number(event.target.value))} />
            ${focus.auto && html`<button type="button" class=${`btn btn-sm ${focus.manual ? '' : 'is-on'}`} title=${t('Automatic focus')}
              aria-pressed=${String(!focus.manual)} onClick=${autofocus}>${t('Auto')}</button>`}
          </div>`}
        </div>`}
      </div>
    </div>
  </div>`;
}

// --- left side: projects, conversation, history ------------------------------

const IMAGE_FILE = /\.(png|jpe?g|webp|gif)$/i;
const PICTURE_TYPES = 'image/png,image/jpeg,image/webp,image/gif'; // the pictures the model can look at

// A spot picked on the model, in words.
function spotText(spot) {
  return spot.kind === 'scan' ? t('STL {name}', { name: spot.name }) : t('part {name}', { name: spot.name });
}

const MENTION = /(@[A-Za-z0-9_][A-Za-z0-9_.-]*)/;

// The files of the open project a prompt can name with @: its models and its STLs.
function mentionable(project) {
  return [
    ...project.files.map((file) => ({ name: bare(file.name), src: modelThumb(project, file), show: () => attempt(() => showModel(file.name)) })),
    ...project.scans.map((scan) => ({ name: scan.name, src: scanThumb(project, scan), scan: true, show: () => showScan(scan.name) })),
  ];
}

// The text of a prompt with every @name of a file that exists as a link to it.
function withMentions(text) {
  const project = state.project;
  if (!project || !String(text ?? '').includes('@')) return text;
  const known = new Map(mentionable(project).map((item) => [item.name, item]));
  return text.split(MENTION).map((piece, index) => {
    if (index % 2 === 0) return piece;
    let name = piece.slice(1);
    let rest = '';
    // "@lid." at the end of a sentence still means lid.
    while (name && !known.has(name) && /[.-]$/.test(name)) {
      rest = name.slice(-1) + rest;
      name = name.slice(0, -1);
    }
    if (!known.has(name)) return piece;
    return html`<button type="button" class="mention" onClick=${known.get(name).show}>@${name}</button>${rest}`;
  });
}

// A prompt as sent: its text, the spot it is about, thumbnails of the pictures,
// chips for other files.
function PromptBlock({ text, attachments, turn, pick }) {
  const projectId = state.projectId;
  const url = (name) => `/api/projects/${projectId}/attachments/${turn}/${encodeURIComponent(name)}`;
  const images = turn ? (attachments ?? []).filter((name) => IMAGE_FILE.test(name)) : [];
  const others = (attachments ?? []).filter((name) => !images.includes(name));
  return html`<div class="prompt">${withMentions(text)}
    ${pick && html`<div class="chips"><span class="chip" title=${t('Point picked on the model')}><${Icon} name="pin" /><span>${spotText(pick)}</span></span></div>`}
    ${images.length > 0 && html`<div class="thumbs">${images.map((name) => html`<a key=${name} href=${url(name)} target="_blank" rel="noopener" title=${name}>
      <img src=${url(name)} alt=${name} loading="lazy" />
    </a>`)}</div>`}
    ${others.length > 0 && html`<div class="chips">${others.map((name) => html`<span class="chip" key=${name}><${Icon} name="clip" /><span>${name}</span></span>`)}</div>`}
  </div>`;
}

const mono = (text) => html`<span class="mono">${text}</span>`;

function Entry({ entry, now, models, onReuse }) {
  if (entry.kind !== 'prompt') {
    const texts = {
      create: ['box', t('Model created')],
      import: ['upload', t('Model imported')],
      param: ['sliders', `${paramLabel(entry.name, entry.label)}: ${localized(entry.from)} → ${localized(entry.to)}`],
      restore: ['history', t('Restored the version from {time}', { time: stamp(entry.time ?? entry.ts, now) })],
      undo: ['undo', t('Undid the last change')],
      redo: ['undo', t('Brought the undone change back')],
      rename: ['pencil', tx('Renamed from {name}', { name: mono(bare(entry.from)) })],
    };
    const [icon, what] = texts[entry.kind] ?? ['box', entry.kind];
    return html`<div class="event" title=${exact(entry.ts)}>
      <${Icon} name=${icon} /><span class="what">${what}</span><span>${when(entry.ts, now)}</span>
    </div>`;
  }
  const stats = entry.stats;
  const model = models.find((m) => m.id === entry.model)?.label ?? entry.model;
  const used = stats ? stats.in + stats.cacheRead + stats.cacheWrite + stats.out : 0;
  const breakdown = stats && t('input {input} ({cached} from cache), output {output}, {requests} requests', { input: stats.in + stats.cacheWrite + stats.cacheRead, cached: stats.cacheRead, output: stats.out, requests: stats.requests });
  return html`<div class="entry">
    <${PromptBlock} text=${entry.text} attachments=${entry.attachments} turn=${entry.turn} pick=${entry.pick} />
    ${entry.reply && html`<div class="reply" dangerouslySetInnerHTML=${{ __html: rich(entry.reply) }} />`}
    ${entry.status === 'reverted' && html`<div class="notice is-danger"><${Icon} name="alert" /><div>
      ${t('The model did not build after this change, so it went back to the previous version.')}
      ${entry.buildError && html`<div class="mono" style="font-size:12px;margin-top:2px">${translate(entry.buildError)}</div>`}
    </div></div>`}
    ${entry.error && html`<div class="notice is-danger"><${Icon} name="alert" /><div>
      ${translate(entry.error)}
    </div></div>`}
    ${entry.stopped && html`<div class="notice"><${Icon} name="stop" /><div class="grow">
      ${t('Stopped.')}${entry.status === 'changed' ? ` ${t('The changes made up to that point were saved.')}` : ''}
    </div><button type="button" class="link" onClick=${() => onReuse(entry.text)}>${t('Fix the prompt')}</button></div>`}
    ${entry.viaApiKey && html`<div class="notice is-danger"><${Icon} name="alert" /><div>${t('This reply was billed to an API key, not to the subscription.')}</div></div>`}
    <div class="meta">
      <span title=${exact(entry.ts)}>${when(entry.ts, now)}</span>
      <span class="usage" title=${[used > 0 && breakdown, stats?.previewMs && t('preview after {time}', { time: seconds(stats.previewMs) })].filter(Boolean).join('\n') || undefined}>
        ${model}${used > 0 ? ` ${compact(used)}` : ''}${stats ? `${used > 0 ? ' / ' : ' '}${Math.max(1, Math.round(stats.ms / 1000))}s` : ''}
      </span>
    </div>
  </div>`;
}

// What the empty conversation invites to, by what the project holds.
function invitation(project) {
  if (project.good?.ok) {
    return {
      title: t('What should change in this model?'),
      text: t('Describe it in your own words. After each change I rebuild the model, show it on the right and save a version in the history.'),
      suggestions: SUGGESTIONS.model.map((text) => t(text)),
    };
  }
  if (project.scans.length && state.info?.engine?.scan) {
    const only = project.scans.length === 1;
    return {
      title: only ? t('Recreate this STL?') : t('Recreate one of the STL files?'),
      text: t('I will measure the mesh (planes, holes, sections) and build a model with parameters from it, then compare it with the STL.'),
      suggestions: [only ? t(SUGGESTIONS.scan[0]) : t('Recreate {name} as a parametric model', { name: project.scans[0].name }), t(SUGGESTIONS.scan[1])],
    };
  }
  // Words are one way to say it; an STL to recreate (where it can be measured), a drawing or a photo are others.
  const scan = state.info?.engine?.scan === true;
  return {
    title: t('What should be made?'),
    scan,
    sketch: true,
    text: project.scans.length
      ? t('I cannot measure STL files in this installation, so describe the object: dimensions, holes, what it has to fit.')
      : t('Describe the object: dimensions, holes, what it has to fit. I will make a model that you can then change with further prompts.'),
    suggestions: (scan ? SUGGESTIONS.blank.slice(0, 1) : SUGGESTIONS.blank).map((text) => t(text)),
  };
}

function Timeline({ project, now, models, onSuggest, onCamera, onPicture, onScan }) {
  const ref = useRef(null);
  const stick = useRef(true);
  // A turn shows here when it is about the open model (or the model it is creating).
  const turn = project.turn && (project.turn.file === project.file || !project.file) ? project.turn : null;
  const elsewhere = project.turn && !turn ? project.turn : null;
  useLayoutEffect(() => {
    if (stick.current && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  });
  const onScroll = () => {
    const node = ref.current;
    stick.current = node.scrollHeight - node.scrollTop - node.clientHeight < 60;
  };
  const prompts = project.journal.filter((entry) => entry.kind === 'prompt').length;
  const invite = !turn && prompts === 0 ? invitation(project) : null;
  return html`<div class="timeline" ref=${ref} onScroll=${onScroll}>
    ${project.journal.map((entry) => html`<${Entry} key=${entry.id} entry=${entry} now=${now} models=${models} onReuse=${onSuggest} />`)}
    ${turn && html`<div class="entry">
      <${PromptBlock} text=${turn.text} attachments=${turn.attachments} turn=${turn.id} pick=${turn.pick} />
      ${turn.reply && html`<div class="reply" dangerouslySetInnerHTML=${{ __html: rich(turn.reply) }} />`}
      <div class="working" role="status"><span class="spinner" />${phaseText(turn.phase)}${turn.tokens > 0 && html`<span class="working-tokens">${compact(turn.tokens)}</span>`}</div>
    </div>`}
    ${elsewhere && html`<div class="working" role="status"><span class="spinner" />
      <span class="grow">${tx('Working on {name}', { name: mono(bare(elsewhere.file)) })}</span>
      <button type="button" class="link" onClick=${() => openFile(elsewhere.file)}>${t('Show')}</button>
    </div>`}
    ${invite && html`<div class="empty-chat">
      <h3>${invite.title}</h3>
      <p>${invite.text}</p>
      ${invite.suggestions.map((text) => html`<button type="button" class="suggestion" key=${text} onClick=${() => onSuggest(text)}>${text}</button>`)}
      ${invite.scan && html`<button type="button" class="suggestion is-action" onClick=${onScan}>
        <${Icon} name="upload" />${t('Recreate an STL file as a parametric model')}
      </button>`}
      ${invite.sketch && CAN_SHOOT && html`<button type="button" class="suggestion is-action" onClick=${() => onCamera(true)}>
        <${Icon} name="camera" />${t('Take a photo of a sketch')}
      </button>`}
      ${invite.sketch && html`<button type="button" class="suggestion is-action" onClick=${onPicture}>
        <${Icon} name="picture" />${t('Add a photo or a sketch from a file')}
      </button>`}
    </div>`}
  </div>`;
}

// History as a time axis, newest on top. A prompt is text on the line with the
// point of its result above it; everything else (import, parameter, restore)
// is a point of its own. A click on a point shows the model as it was then.
function History({ project, peek, now }) {
  const { history } = project;
  // The right mouse button on a version: what else can be done with it.
  const [menu, setMenu] = useState(null); // { hash, x, y }
  const ref = useDismiss(!!menu, () => setMenu(null));
  const branch = (hash) => {
    setMenu(null);
    attempt(async () => {
      const { file } = await api(`/projects/${project.id}/files`, { name: `${bare(project.file)}_copy`, copyOf: project.file, rev: hash });
      await openFile(file);
    }, t('New model created'));
  };
  return html`<div class="timeline axis" onScroll=${() => menu && setMenu(null)}>
    ${menu && html`<div ref=${ref} class="menu axis-menu" role="menu" style=${`left:${menu.x}px;top:${menu.y}px`}>
      <button type="button" role="menuitem" class="menu-item" disabled=${!!project.turn} onClick=${() => branch(menu.hash)}><${Icon} name="copy" />${t('New model from this version')}</button>
    </div>`}
    ${history.map((entry, index) => {
      const isHead = entry.hash === project.head;
      const peeked = peek?.rev === entry.hash;
      const shown = peeked || (isHead && !peek);
      const minor = entry.kind !== 'prompt';
      const show = () => (isHead || peeked ? setState({ peek: null }) : peekVersion(entry));
      return html`<div key=${entry.hash}>
        <div class=${`axis-point ${shown ? 'is-shown' : ''} ${isHead ? 'is-head' : ''} ${minor ? 'is-minor' : ''} ${minor && index === history.length - 1 ? 'is-first' : ''}`} role="button" tabindex="0" aria-pressed=${String(shown)} title=${exact(entry.time)}
          onContextMenu=${(e) => { e.preventDefault(); setMenu({ hash: entry.hash, x: e.clientX, y: e.clientY }); }}
          onClick=${show} onKeyDown=${(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); show(); } }}>
          <span class="dot" />
          ${minor
            ? html`<span class="axis-what ellipsis">${bare(translate(entry.subject))}</span><span class="axis-when">${isHead ? t('now') : when(entry.time, now)}</span>`
            : html`<span class="axis-when">${isHead ? t('now') : when(entry.time, now)}</span>`}
          ${!isHead && html`<button type="button" class="btn btn-sm axis-restore" disabled=${!!project.turn}
            onClick=${(e) => { e.stopPropagation(); restoreVersion(entry.hash); }} title=${t('The model goes back to this version. Nothing is lost: the later versions stay in the history.')}>${t('Restore this version')}</button>`}
        </div>
        ${!minor && html`<div class=${`axis-change ${index === history.length - 1 ? 'is-first' : ''}`}>
          <span class="axis-text">${bare(entry.subject)}</span>
        </div>`}
      </div>`;
    })}
    ${history.length === 0 && html`<div class="empty-chat"><p>${t('This model has no versions yet. The first one will appear after the first prompt.')}</p></div>`}
  </div>`;
}

// How much of the subscription's allowance is used, as Claude Code last reported it.
function Limits({ limits, now }) {
  const window = limits?.fiveHour ?? limits?.week;
  if (!window || (window.resetsAt && window.resetsAt < now)) return null;
  const percent = (share) => `${Math.round(share.used * 100)}%`;
  const detail = [
    limits.fiveHour && t('5 h window: {percent}, resets at {time}.', { percent: percent(limits.fiveHour), time: stamp(limits.fiveHour.resetsAt, now) }),
    limits.week && t('Week: {percent}, resets {time}.', { percent: percent(limits.week), time: stamp(limits.week.resetsAt, now) }),
    t('Subscription usage as reported by Claude Code.'),
  ].filter(Boolean).join(' ');
  return html`<span class=${`limits ${window.used >= 0.8 ? 'is-high' : ''}`} title=${detail}>
    ${limits.fiveHour ? t('5 h limit: {percent}', { percent: percent(window) }) : t('weekly limit: {percent}', { percent: percent(window) })}
  </span>`;
}

// --- accounts and models -----------------------------------------------------

// Who is signed in can change while the page is open (a sign-in finishing in
// the browser): ask again, and keep the chosen model only while it exists.
async function refreshAccounts() {
  const info = await api('/state');
  setState({ info, model: info.models.some((m) => m.id === state.model) ? state.model : info.defaultModel });
  return info;
}

// The models side by side, one column per account, one row per size. Each
// column says how that account is signed in on this computer; the app only reads that.
function ModelPicker({ info, model }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const providers = info.providers ?? [];
  const current = info.models.find((m) => m.id === model);
  // Somebody may have signed in from a terminal meanwhile: asking makes the app look again.
  useEffect(() => {
    if (!open) return undefined;
    refreshAccounts().catch(() => {});
    const again = setTimeout(() => refreshAccounts().catch(() => {}), 4000);
    return () => clearTimeout(again);
  }, [open]);
  const head = (provider) => html`<div class="picker-head" key=${provider.id}>
    <div class="picker-name">${provider.name}</div>
    <div class="picker-note">${!provider.installed ? html`${t('not installed')}: <a href=${provider.install} target="_blank" rel="noreferrer">${t('how to install')}</a>`
      : provider.signedIn ? (provider.plan ? t('plan {plan}', { plan: provider.plan }) : t('signed in'))
      : provider.otherSignIn ? t('signed in with {method}, not with a subscription', { method: provider.otherSignIn })
      : tx('not signed in: sign in with {command} in a terminal', { command: mono(provider.command) })}</div>
  </div>`;

  return html`<div class="picker" ref=${ref}>
    <button type="button" class="select picker-button" aria-haspopup="dialog" aria-expanded=${String(open)} title=${translate(current?.hint)} onClick=${() => setOpen(!open)}>
      ${current ? current.label : t('No AI model')}
    </button>
    ${open && html`<div class="menu picker-menu" role="dialog" aria-label=${t('AI model')} style=${`--columns:${providers.length || 1}`}>
      ${providers.map(head)}
      ${(info.tiers ?? []).map((tier) => html`
        ${providers.map((provider) => {
          const offered = info.models.find((m) => m.provider === provider.id && m.tier === tier.id);
          return html`<button type="button" key=${`${tier.id}/${provider.id}`} class=${`picker-model ${offered && offered.id === model ? 'is-current' : ''}`} disabled=${!offered}
            aria-pressed=${String(!!offered && offered.id === model)} onClick=${() => { setState({ model: offered.id }); setOpen(false); }}>
            ${offered ? offered.label : '–'}
          </button>`;
        })}
        <div class="picker-hint" key=${`${tier.id}/hint`}>${translate(tier.hint)}</div>`)}
    </div>`}
  </div>`;
}

function Composer({ project, info, model, draft, setDraft, files, setFiles, onCamera, dragging, limits, now }) {
  const box = useRef(null);
  const picker = useRef(null);
  const busy = !!project.turn;

  useLayoutEffect(() => {
    const node = box.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${node.scrollHeight}px`;
  }, [draft]);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy || !model) return;
    const sent = { draft, files };
    setDraft('');
    setFiles([]);
    setState({ tab: 'chat', listing: false, peek: null }); // the answer arrives in the conversation
    const ok = await attempt(() => api(`/projects/${project.id}/prompt`, { file: project.file, text, model, files: sent.files }));
    if (!ok) {
      setDraft(sent.draft);
      setFiles(sent.files);
    }
  };

  // While an @name is being typed: the files it can become, to pick from.
  const [mention, setMention] = useState(null); // { start, end, options, index }
  const look = (node) => {
    const end = node.selectionStart;
    const found = /(?:^|\s)@([A-Za-z0-9_.-]*)$/.exec(node.value.slice(0, end));
    const query = found?.[1].toLowerCase();
    const options = found ? mentionable(project).filter((item) => item.name !== bare(project.file) && item.name.toLowerCase().includes(query)).slice(0, 8) : [];
    setMention(options.length ? { start: end - found[1].length - 1, end, options, index: 0 } : null);
  };
  const pickMention = (item) => {
    const text = `${draft.slice(0, mention.start)}@${item.name} ${draft.slice(mention.end)}`;
    const caret = mention.start + item.name.length + 2;
    setDraft(text);
    setMention(null);
    requestAnimationFrame(() => {
      box.current?.focus();
      box.current?.setSelectionRange(caret, caret);
    });
  };
  const mentionKey = (e) => {
    if (!mention) return false;
    const step = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0;
    if (step) setMention({ ...mention, index: (mention.index + step + mention.options.length) % mention.options.length });
    else if ((e.key === 'Enter' || e.key === 'Tab') && !e.isComposing) pickMention(mention.options[mention.index]);
    else if (e.key === 'Escape') { e.stopPropagation(); setMention(null); }
    else return false;
    e.preventDefault();
    return true;
  };

  const placeholder = dragging ? t('Drop the files here')
    : project.good?.ok ? t('Describe the change. Type @ to point at another model')
    : t('Describe what to make. Type @ to point at another model');

  return html`<div class="composer-wrap">
    <div class=${`composer ${dragging ? 'is-over' : ''}`}>
      ${files.length > 0 && html`<div class="chips">${files.map((file, index) => html`<span class="chip" key=${`${file.name}${index}`}>
        ${file.type?.startsWith('image/') ? html`<img class="chip-thumb" src=${`data:${file.type};base64,${file.data}`} alt="" />` : html`<${Icon} name="clip" />`}<span>${file.name}</span>
        <button type="button" aria-label=${t('Remove {name}', { name: file.name })} onClick=${() => setFiles(files.filter((_, i) => i !== index))}><${Icon} name="x" /></button>
      </span>`)}</div>`}
      <textarea ref=${box} rows="1" value=${draft} aria-label=${t('Prompt')} placeholder=${placeholder}
        onInput=${(e) => { setDraft(e.target.value); look(e.target); }}
        onClick=${(e) => look(e.target)} onBlur=${() => setTimeout(() => setMention(null), 150)}
        onKeyUp=${(e) => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') look(e.target); }}
        onKeyDown=${(e) => {
          if (mentionKey(e)) return;
          if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            send();
          }
        }} />
      ${mention && html`<div class="menu mention-menu" role="listbox" aria-label=${t('Files of this project')}>
        ${mention.options.map((item, index) => html`<button type="button" role="option" key=${item.name} aria-selected=${String(index === mention.index)}
          class=${`menu-item ${index === mention.index ? 'is-active' : ''}`} onMouseDown=${(e) => { e.preventDefault(); pickMention(item); }}>
          <${Thumb} src=${item.src} scan=${item.scan} /><span class="ellipsis">${item.name}</span>
        </button>`)}
      </div>`}
      <div class="composer-row">
        <${IconButton} icon="clip" label=${t('Attach files (pictures, drawings, notes, STL)')} onClick=${() => picker.current.click()} />
        <input ref=${picker} type="file" multiple hidden onChange=${(e) => { bringFiles(e.target.files, files, setFiles, setDraft); e.target.value = ''; }} />
        ${CAN_SHOOT && html`<${IconButton} icon="camera" label=${t('Take a photo (a sketch, a part, a drawing)')} onClick=${() => onCamera()} />`}
        <${ModelPicker} info=${info} model=${model} />
        <span class="grow" />
        ${busy
          ? html`<button type="button" class="send" title=${t('Stop')} aria-label=${t('Stop')} onClick=${() => attempt(() => api(`/projects/${project.id}/stop`, {}))}><${Icon} name="stop" /></button>`
          : html`<button type="button" class="send" title=${t('Send (Enter)')} aria-label=${t('Send')} disabled=${!draft.trim() || !model} onClick=${send}><${Icon} name="up" /></button>`}
      </div>
    </div>
  </div>`;
}

// STL files added from a conversation are named in its prompt at once, ready to be
// talked about. One alone, in a model that is still empty, comes with the obvious request.
function nameScans(names, setDraft) {
  if (!names.length) return;
  const { project, info } = state;
  setDraft((was) => {
    if (!was.trim() && names.length === 1 && info?.engine?.scan && !project?.good?.ok) return t('Recreate @{name} as a parametric model', { name: names[0] });
    return `${was}${was && !/\s$/.test(was) ? ' ' : ''}${names.map((name) => `@${name}`).join(' ')} `;
  });
  requestAnimationFrame(() => {
    const box = document.querySelector('.composer textarea');
    box?.focus();
    box?.setSelectionRange(box.value.length, box.value.length);
  });
}

// Files brought into a conversation. An STL joins the STL files of the project and
// is named in the prompt; anything else goes with the prompt as an attachment.
async function bringFiles(list, files, setFiles, setDraft) {
  const picked = [...list];
  const meshes = picked.filter(isScanFile);
  const rest = picked.filter((file) => !isScanFile(file));
  if (rest.length) addFiles(rest, files, setFiles);
  if (!meshes.length) return;
  if (state.project?.turn) toast(t('Wait until I finish'), { error: true });
  else nameScans(await addScans(meshes), setDraft);
}

// Files picked or dropped join those of the next prompt. Resolves to how many were taken.
async function addFiles(list, files, setFiles) {
  const picked = [...list];
  const tooBig = picked.find((file) => file.size > MAX_FILE_BYTES);
  if (tooBig) toast(t('File too large (limit 6 MB)'), { error: true });
  const fits = picked.filter((file) => file.size <= MAX_FILE_BYTES).slice(0, Math.max(0, MAX_FILES - files.length));
  if (picked.length > fits.length && !tooBig) toast(t('At most {count} files', { count: MAX_FILES }), { error: true });
  try {
    setFiles([...files, ...(await Promise.all(fits.map(readFile)))]);
    return fits.length;
  } catch (err) {
    toast(err.message, { error: true });
    return 0;
  }
}

// An STL that is open: what it is, and the one thing to do with it.
function ScanPanel({ project, scan, info, model }) {
  const can = info.engine?.scan === true;
  const [facts, setFacts] = useState(null);
  useEffect(() => {
    let live = true;
    setFacts(null);
    if (can) api(`/projects/${project.id}/scans/${encodeURIComponent(scan.name)}/info`).then((found) => { if (live) setFacts(found); }).catch(() => {});
    return () => { live = false; };
  }, [project.id, scan.name, scan.updatedAt]);
  const size = facts?.size ? `${facts.size.map((side) => number(side, 1)).join(' × ')} mm` : null;
  return html`<div class="timeline"><div class="empty-chat scan-panel">
    <h3>${t('A model with parameters from this STL')}</h3>
    <p>${t('An STL is a finished shape: it has no numbers to change. I can measure it (planes, holes, sections) and build a model that recreates it, with parameters you can change. The STL itself stays as it is, to compare the model with.')}</p>
    ${size && html`<p class="scan-facts">${size}<span>${fileSize(scan.size)}</span></p>`}
    <div class="scan-create">
      <button type="button" class="btn btn-primary" disabled=${!!project.turn || !model || !can} onClick=${() => attempt(() => modelFromScan(scan.name))}>
        <${Icon} name="sparkles" />${t('Create a parametric model')}
      </button>
      <${ModelPicker} info=${info} model=${model} />
    </div>
    ${!can && html`<p>${t('I cannot measure STL files in this installation, so describe the object: dimensions, holes, what it has to fit.')}</p>`}
    ${project.turn && html`<p>${t('Wait until I finish')}</p>`}
  </div></div>`;
}

let tabFrom = null; // where on screen the clicked model tab was, for its slide to the new place

function Side({ s, draft, setDraft, files, setFiles }) {
  const { project, info } = s;
  // A suggestion or an earlier prompt, put into the box ready to edit and send.
  const reuse = (text) => {
    setDraft(text);
    document.querySelector('.composer textarea')?.focus();
  };
  // The camera, for a photo that goes with the next prompt; of a sketch, when that is what was offered.
  const camera = (sketch = false) => {
    if (files.length >= MAX_FILES) toast(t('At most {count} files', { count: MAX_FILES }), { error: true });
    else setState({ dialog: { kind: 'camera', sketch } });
  };
  // A photo or a sketch from the disk, with its request ready to send or to add to.
  const picture = () => {
    const input = Object.assign(document.createElement('input'), { type: 'file', accept: PICTURE_TYPES, multiple: true });
    input.addEventListener('change', async () => {
      if (!(await addFiles(input.files, files, setFiles))) return;
      setDraft((was) => (was.trim() ? was : t('Make a model from the attachment')));
      requestAnimationFrame(() => document.querySelector('.composer textarea')?.focus());
    });
    input.click();
  };
  // An STL from the disk: it joins the project's STL files and its request waits in the prompt.
  const scanFile = () => {
    const input = Object.assign(document.createElement('input'), { type: 'file', accept: '.stl', multiple: true });
    input.addEventListener('change', () => bringFiles(input.files, files, setFiles, setDraft));
    input.click();
  };
  const tab = project?.file ? s.tab : 'chat';
  // The models as a stack of tabs: the list of them, or one of them open with
  // its conversation underneath. A project with no model has nothing to open.
  // An STL that is open takes the panel: it has no conversation, only what can be made from it.
  const scan = project?.scans.find((item) => item.name === s.scanOpen) ?? null;
  const listing = !!project && !scan && (s.listing || s.unpicked || !project.file);
  // A model's tab travels between its place in the list and the top of the panel.
  const side = useRef(null);
  const wasListing = useRef(listing);
  useLayoutEffect(() => {
    if (wasListing.current === listing) return; // the list is still up while the clicked model loads
    wasListing.current = listing;
    const from = tabFrom;
    tabFrom = null;
    const tab = side.current?.querySelector(listing ? '.model-tab.is-current' : '.model-tab.is-open');
    if (from == null || !tab || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timing = { duration: 240, easing: 'cubic-bezier(.2, .8, .2, 1)' };
    tab.animate([{ transform: `translateY(${from - tab.getBoundingClientRect().top}px)` }, { transform: 'none' }], timing);
    for (const node of side.current.querySelectorAll('.side-tabs, .timeline, .params, .overview, .composer-wrap, .model-row, .model-add')) {
      if (!node.contains(tab)) node.animate([{ opacity: 0 }, { opacity: 1 }], timing);
    }
  }, [listing, project?.file]);
  const meta = s.peek ? s.peek.meta : project?.good ?? null;
  // The name at the end of the path is edited where it stands: the open model or STL, or the project.
  const named = project && !listing ? `${scan ? 'scan' : 'file'}:${scan ? scan.name : project.file}` : `project:${s.projectId}`;
  const [naming, setNaming] = useState(null); // which name is being edited, as in `named`
  return html`<aside class="side" ref=${side}>
    <div class="side-head">
      <nav class="crumbs" aria-label=${t('Location')}>
        ${project && !listing
          // Inside a model or an STL the way back leads to the project's list; from the list, to all projects.
          ? html`<button type="button" class="crumb" title=${t('All models')} onClick=${showList}>
              <${Icon} name="chevron" /><span class="crumb-kind">${t('Project')}</span><span class="crumb-root ellipsis">${s.projectId}</span>
            </button>
            <span class="crumb-sep" aria-hidden="true">/</span>
            ${naming === named
              ? html`<${NameEdit} key=${named} value=${(scan ? scan.name : project.file).replace(/\.(py|stl)$/i, '')} onClose=${() => setNaming(null)}
                  onSave=${(name) => renameFile(scan ? scan.name : project.file, name, !!scan)} />`
              : html`<button type="button" class="crumb crumb-rename" title=${t('Rename {name}', { name: scan ? scan.name : bare(project.file) })} disabled=${!!project.turn}
                  onClick=${() => setNaming(named)}>
                  <span class="crumb-name ellipsis">${scan ? scan.name : bare(project.file)}</span><${Icon} name="pencil" />
                </button>`}`
          : html`<button type="button" class="crumb" title=${t('All projects')} onClick=${showHome}>
              <${Icon} name="chevron" /><span class="crumb-root">${t('Projects')}</span>
            </button>
            ${s.projectId && html`<span class="crumb-sep" aria-hidden="true">/</span>
              ${naming === named
                ? html`<${NameEdit} key=${named} value=${s.projectId} hint=${t('Its folder will be renamed too.')} onClose=${() => setNaming(null)}
                    onSave=${(name) => renameProject(s.projectId, name)} />`
                : html`<button type="button" class="crumb crumb-rename" title=${t('Rename the project {name}', { name: s.projectId })} disabled=${!project || !!project.turn}
                    onClick=${() => setNaming(named)}>
                    <span class="crumb-name ellipsis">${s.projectId}</span><${Icon} name="pencil" />
                  </button>`}`}`}
      </nav>
    </div>
    ${listing && html`<${ModelList} project=${project} unpicked=${s.unpicked} dragging=${s.dragging} uploading=${s.uploading} />`}
    ${scan && html`
    <${ScanPanel} project=${project} scan=${scan} info=${info} model=${s.model} />`}
    ${project && !listing && !scan && html`
    <div class="side-tabs">
      <div class="tabs" role="tablist">
        <button type="button" role="tab" aria-selected=${String(tab === 'chat')} onClick=${() => setState({ tab: 'chat' })}>${t('Conversation')}</button>
        <button type="button" role="tab" aria-selected=${String(tab === 'params')} onClick=${() => setState({ tab: 'params' })}>${t('Parameters')}</button>
        <button type="button" role="tab" aria-selected=${String(tab === 'summary')} onClick=${() => setState({ tab: 'summary' })}>${t('Summary')}</button>
        <button type="button" role="tab" aria-selected=${String(tab === 'history')} onClick=${() => setState({ tab: 'history' })}>${t('History')}</button>
      </div>
    </div>`}
    ${project && !listing && !scan && (tab === 'chat'
      ? html`<${Timeline} project=${project} now=${s.now} models=${info.models} onSuggest=${reuse} onCamera=${camera} onPicture=${picture} onScan=${scanFile} />`
      : tab === 'params'
        ? html`<${Params} meta=${meta?.ok ? meta : null} locked=${!!project.turn || !!s.peek} />`
        : tab === 'summary'
        ? html`<${Overview} project=${project} meta=${meta?.ok ? meta : null} codeOpen=${s.options.code} />`
        : html`<${History} project=${project} peek=${s.peek} now=${s.now} />`)}
    ${!project && html`<div class="timeline"><div class="empty-chat">
      ${s.projectId ? html`<div class="working"><span class="spinner" />${t('Opening the project…')}</div>`
        : html`<h3>${t('No project open')}</h3><p>${t('Create a project to get started. It can be empty.')}</p>`}
    </div></div>`}
    ${project && !listing && !scan && html`<${Composer} project=${project} info=${info} model=${s.model} draft=${draft} setDraft=${setDraft}
      files=${files} setFiles=${setFiles} onCamera=${camera} dragging=${s.dragging} limits=${s.limits} now=${s.now} />`}
  </aside>`;
}

// --- right side: the model ---------------------------------------------------

function ParamRow({ param, disabled }) {
  const shown = param.kind === 'value' ? localized(param.raw) : number(param.value, 4);
  const [text, setText] = useState(shown);
  const [saving, setSaving] = useState(false);
  useEffect(() => setText(shown), [shown]);

  const commit = async (raw = text) => {
    const value = Number(String(raw).trim().replace(',', '.'));
    if (String(raw).trim() === '' || !Number.isFinite(value)) {
      setText(shown);
      return;
    }
    if (value === param.value) {
      setText(shown);
      return;
    }
    setSaving(true);
    const ok = await attempt(() => api(filePath('param'), { name: param.name, value }));
    setSaving(false);
    if (!ok) setText(shown);
  };
  const nudge = (event) => {
    if (event.key === 'Enter') {
      event.target.blur();
    } else if (event.key === 'Escape') {
      setText(shown);
      event.target.blur();
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      const current = Number(text.replace(',', '.'));
      if (!Number.isFinite(current)) return;
      const whole = /^[-+]?\d+$/.test(param.raw);
      const step = (whole ? 1 : 0.1) * (event.shiftKey ? 10 : 1) * (event.key === 'ArrowUp' ? 1 : -1);
      const next = Number((current + step).toFixed(6));
      setText(localized(next));
      commit(next);
    }
  };
  const derived = param.kind !== 'value';
  const label = paramLabel(param.name, param.comment);
  return html`<label class=${`param ${derived ? 'is-derived' : ''} ${saving ? 'is-saving' : ''}`} title=${t('{label} (in the file: {name})', { label, name: param.name })}>
    <span class="name">${label}</span>
    <input value=${text} inputmode="decimal" disabled=${disabled || derived}
      onInput=${(e) => setText(e.target.value)} onBlur=${() => commit()} onKeyDown=${nudge} />
  </label>`;
}

function Params({ meta, locked }) {
  const [filter, setFilter] = useState('');
  const [derived, setDerived] = useState(false);
  const all = meta?.params ?? [];
  const editable = all.filter((p) => p.kind === 'value');
  const needle = filter.trim().toLowerCase();
  const shown = all.filter((p) => (derived || p.kind === 'value')
    && (!needle || p.name.toLowerCase().includes(needle) || (p.comment ?? '').toLowerCase().includes(needle)));
  let lastGroup;
  return html`<section class="params" aria-label=${t('Parameters')}>
    ${all.length > 10 && html`<div class="params-filter"><input class="input" placeholder=${t('Search parameters')} value=${filter} onInput=${(e) => setFilter(e.target.value)} /></div>`}
    <div class="params-body">
      ${shown.length === 0 && html`<div class="param-group">${all.length ? t('Nothing matches.') : meta ? t('This model has no parameters yet. Ask for one in the conversation, e.g. "make the wall thickness a parameter".') : t('Parameters appear once the model builds.')}</div>`}
      ${shown.map((param) => {
        const header = param.group !== lastGroup && param.group;
        lastGroup = param.group;
        return html`${header && html`<div class="param-group" title=${param.group} key=${`g${param.line}`}>${capitalized(param.group)}</div>`}
          <${ParamRow} key=${param.name} param=${param} disabled=${locked} />`;
      })}
    </div>
    ${all.length > editable.length && html`<div class="params-foot"><label>
      <input type="checkbox" checked=${derived} onChange=${(e) => setDerived(e.target.checked)} />${t('Also show computed values')}
    </label></div>`}
  </section>`;
}

function describeCheck(check) {
  if (check.kind === 'valid') return t('the solid is open or intersects itself');
  if (check.kind === 'scan') return t('there is no such STL in the project');
  // The detail comes from the build in English, as a few fixed words around numbers and names.
  return check.detail.replace('overlap', t('overlap')).replace('gap', t('gap')).replace('needs', t('needs')).replace('no part named', t('no part named'));
}

// The model's own checks, shown only when one of them fails.
function Checks({ meta }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const checks = meta.checks ?? [];
  const failing = checks.filter((check) => !check.ok).length;
  if (!failing) return null;
  return html`<span ref=${ref} style="position:relative">
    <button type="button" class="checks-button is-failing" aria-expanded=${String(open)} onClick=${() => setOpen(!open)}>
      <${Icon} name="alert" />
      ${t('{failing} of {total} checks failing', { failing, total: checks.length })}
    </button>
    ${open && html`<div class="menu checks-list">
      ${checks.map((check) => html`<div class=${`check-row ${check.ok ? '' : 'is-failing'}`} key=${check.name}>
        <${Icon} name=${check.ok ? 'check' : 'x'} />
        <div><div class="name">${check.name}</div><div class="muted">${check.a === check.b ? check.a : `${check.a} / ${check.b}`}: ${describeCheck(check)}</div></div>
      </div>`)}
    </div>`}
  </span>`;
}

// How close the model is to a scan it reproduces: the share of compared points
// within tolerance, the worse of the two directions.
function ScanFit({ scan }) {
  const deviation = scan.deviation;
  if (!deviation) return scan.text ? html`<span class="fit is-low" title=${translate(scan.text)}>${t('STL {name}: not compared', { name: scan.name })}</span>` : null;
  const sides = [deviation.scan_to_model, deviation.model_to_scan].filter((side) => typeof side?.within === 'number');
  if (!sides.length) return null;
  const share = Math.min(...sides.map((side) => side.within));
  const far = Math.max(...sides.map((side) => side.max ?? 0));
  const percent = Math.round(share * 100);
  const detail = `${t('Points within {tol} mm: {percent}%. Largest deviation {far} mm.', { tol: number(deviation.tol ?? 0.5), percent, far: number(far) })} ${t('The STL can be drawn over the model with the button above the view.')}`;
  return html`<span class=${`fit ${share < 0.9 ? 'is-low' : ''}`} title=${detail}>${t('match with {name}: {percent}%', { name: scan.name, percent })}</span>`;
}

// On the stage: only what needs attention (a failing check, a poor match with a scan).
function Summary({ meta }) {
  return html`<div class="summary"><div class="facts">
    ${(meta.scans ?? []).map((scan) => html`<${ScanFit} key=${scan.name} scan=${scan} />`)}
    <${Checks} meta=${meta} />
  </div></div>`;
}

const PLA_DENSITY = 1.24; // g/cm3
const SPAN_NAMES = { wall: 'Wall', gap: 'Gap', face: 'Width', step: 'Step' };
const sizeOf = (bbox) => [0, 1, 2].map((i) => bbox[1][i] - bbox[0][i]);
const mm = (value) => `${formatMm(value)} mm`;

function Fact({ label, value, note }) {
  return html`<div class="fact"><span class="fact-label">${label}</span><span class="fact-value">${value}</span>${note && html`<span class="fact-note">${note}</span>`}</div>`;
}

// The Summary tab: every number about the model in one place, and its files.
function Overview({ project, meta, codeOpen }) {
  const actions = html`<div class="overview-actions">
    <button type="button" class="btn btn-sm" aria-pressed=${String(!!codeOpen)} onClick=${() => setState((st) => ({ options: { ...st.options, code: !st.options.code } }))}><${Icon} name="code" />${t('Code')}</button>
  </div>`;
  if (!meta) return html`<section class="overview">${actions}<div class="param-group">${t('The numbers appear once the model builds.')}</div></section>`;

  const [x, y, z] = sizeOf(meta.bbox);
  const cm3 = meta.volume / 1000;
  const printed = meta.parts.filter((part) => part.kind === 'show');
  const holes = new Map(); // "diameter|depth" -> count
  for (const hole of printed.flatMap((part) => part.holes ?? [])) {
    const key = `${formatMm(hole.d)}|${formatMm(hole.depth)}`;
    holes.set(key, (holes.get(key) ?? 0) + 1);
  }
  const spans = [];
  for (const span of printed.flatMap((part) => part.spans ?? [])) {
    const text = `${span.kind}|${formatMm(span.value)}`;
    if (!spans.some((item) => item.text === text)) spans.push({ text, kind: span.kind, value: span.value });
  }
  return html`<section class="overview">
    ${actions}
    <div class="param-group">${t('Size')}</div>
    <${Fact} label=${t('Width (X)')} value=${mm(x)} />
    <${Fact} label=${t('Depth (Y)')} value=${mm(y)} />
    <${Fact} label=${t('Height (Z)')} value=${mm(z)} />

    <div class="param-group">${t('Material')}</div>
    <${Fact} label=${t('Volume')} value=${`${number(cm3)} cm³`} />
    <${Fact} label=${t('Filament (PLA)')} value=${`${number(cm3 * PLA_DENSITY, 1)} g`} note=${t('if printed solid')} />

    ${printed.length > 1 && html`<div class="param-group">${t('Parts')}</div>
      ${printed.map((part) => html`<${Fact} key=${part.name} label=${part.name} value=${`${sizeOf(part.bbox).map((v) => formatMm(v)).join(' × ')} mm`} note=${`${number(part.volume / 1000)} cm³`} />`)}`}

    ${holes.size > 0 && html`<div class="param-group">${t('Holes')}</div>
      ${[...holes].map(([key, count]) => {
        const [d, depth] = key.split('|');
        return html`<${Fact} key=${key} label=${`${count} × ⌀ ${d} mm`} value=${t('depth {depth} mm', { depth })} />`;
      })}`}

    ${(spans.length > 0 || (meta.dims ?? []).length > 0) && html`<div class="param-group">${t('Dimensions')}</div>
      ${(meta.dims ?? []).map((dim) => html`<${Fact} key=${`d${dim.label}`} label=${dim.label} value=${mm(dim.value)} />`)}
      ${spans.map((span) => html`<${Fact} key=${span.text} label=${t(SPAN_NAMES[span.kind] ?? SPAN_NAMES.face)} value=${mm(span.value)} />`)}`}
  </section>`;
}

function CodeView({ project, peek }) {
  const [text, setText] = useState(null);
  const [failed, setFailed] = useState(false);
  const rev = peek?.rev ?? null;
  useEffect(() => {
    let live = true;
    setText(null);
    setFailed(false);
    fetch(`/api${filePath('source')}${rev ? `?rev=${rev}` : ''}`)
      .then((res) => (res.ok ? res.text() : Promise.reject()))
      .then((body) => { if (live) setText(body); })
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [project.id, project.file, project.head, project.build?.key, rev]);
  // The page re-renders on every event; the lines are laid out once per text.
  const lines = useMemo(() => (text ? text.split('\n').map((line, index) => html`<div key=${index}><span class="ln">${index + 1}</span>${line}</div>`) : null), [text]);
  return html`<section class="code-view" aria-label=${t('Model code')}>
    <div class="code-head">
      <span class="mono">${bare(project.file)}</span><span class="muted">${rev ? t('older version, read-only') : t('read-only')}</span><span class="grow" />
      <button type="button" class="btn btn-ghost btn-sm" onClick=${() => attempt(() => api(filePath('open'), { what: 'source' }))}><${Icon} name="external" />${t('Open in editor')}</button>
      <${IconButton} icon="x" label=${t('Close code')} onClick=${() => setState((s) => ({ options: { ...s.options, code: false } }))} />
    </div>
    <pre>${failed ? t('Could not load the file.') : text == null ? t('Loading…') : text === '' ? html`<div class="muted">${t('The file is empty.')}</div>` : lines}</pre>
  </section>`;
}

// The project's files in two lists: its models (each with a conversation), and
// the STL files models can be made from.
function ModelList({ project, unpicked, dragging, uploading }) {
  const picked = unpicked ? null : project.file;
  const [renaming, setRenaming] = useState(null); // the row whose name is being edited: { name, scan }
  const locked = !!project.turn;
  const open = (show, name) => (event) => {
    tabFrom = event.currentTarget.getBoundingClientRect().top;
    attempt(() => show(name));
  };
  // The row of a file while its name is edited: the same row, with a field where the name was.
  const editing = (name, scan, src) => html`<div class=${`model-tab is-form ${scan ? 'is-scan' : ''}`} key=${scan ? `scan:${name}` : name}>
    <${Thumb} src=${src} scan=${scan} />
    <${NameEdit} value=${name.replace(/\.(py|stl)$/i, '')} onSave=${(to) => renameFile(name, to, scan)} onClose=${() => setRenaming(null)} />
  </div>`;
  const removeModel = (name) => setState({
    dialog: {
      kind: 'confirm',
      title: t('Delete {name}?', { name: bare(name) }),
      lead: t("The model disappears from the project together with its conversation. Its versions stay in the project's git history."),
      label: t('Delete model'),
      action: () => api(`/projects/${project.id}/files/${encodeURIComponent(name)}/delete`, {}),
    },
  });
  const removeScan = (name) => setState({
    dialog: {
      kind: 'confirm',
      title: t('Delete {name}?', { name }),
      lead: t('Models that recreate it will no longer be compared with it.'),
      label: t('Delete STL'),
      action: () => fetch(`${scanPath(project.id, name)}/delete`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(reply),
    },
  });
  const canCreate = !locked && !!state.model && state.info?.engine?.scan === true;

  return html`<div class=${`model-list ${state.opening ? 'is-loading' : ''} ${dragging ? 'is-over' : ''}`}>
    <div class="list-head">
      <span class="list-label">${t('Models')}</span>
      <button type="button" class="list-add" disabled=${locked} title=${t('New model')} onClick=${() => attempt(newModel)}><${Icon} name="plus" /><span>${t('New')}</span></button>
    </div>
    ${project.files.length === 0 && html`<p class="list-empty">${t('No model yet. Start an empty one, or make one from an STL below.')}</p>`}
    ${project.files.map((file) => (renaming?.name === file.name && !renaming.scan ? editing(file.name, false, modelThumb(project, file))
      : html`<div class="model-row" key=${file.name}>
          <button type="button" class=${`model-tab ${file.name === picked ? 'is-current' : ''}`} aria-expanded="false" onClick=${open(showModel, file.name)}>
            <${Thumb} src=${modelThumb(project, file)} /><span class="grow ellipsis">${bare(file.name)}</span>
            ${project.turn?.file === file.name && html`<span class="spinner" />`}
          </button>
          <span class="row-actions">
            <${IconButton} icon="pencil" label=${t('Rename {name}', { name: bare(file.name) })} disabled=${locked} onClick=${() => setRenaming({ name: file.name, scan: false })} />
            <${IconButton} icon="copy" label=${t('Duplicate {name}', { name: bare(file.name) })} disabled=${locked} onClick=${() => attempt(() => api(`/projects/${project.id}/files`, { name: `${bare(file.name)}_copy`, copyOf: file.name }), t('Duplicated'))} />
            <${IconButton} icon="trash" label=${t('Delete {name}', { name: bare(file.name) })} disabled=${locked} onClick=${() => removeModel(file.name)} />
          </span>
        </div>`))}
    <div class="list-head">
      <span class="list-label">${t('STL files')}</span>
      <button type="button" class="list-add" disabled=${locked} title=${t('Load an STL file from the disk')} onClick=${chooseScans}>
        ${uploading ? html`<span class="spinner" />` : html`<${Icon} name="upload" />`}<span>${t('Load STL')}</span>
      </button>
    </div>
    ${project.scans.length === 0 && html`<p class="list-empty">${t('An STL is a finished shape without parameters. Load one to make a parametric model from it, or to take its dimensions when designing a part that has to fit it.')}</p>`}
    ${project.scans.map((scan) => (renaming?.name === scan.name && renaming.scan ? editing(scan.name, true, scanThumb(project, scan))
      : html`<div class="model-row" key=${`scan:${scan.name}`}>
        <button type="button" class="model-tab is-scan" aria-expanded="false" onClick=${open(showScan, scan.name)}>
          <${Thumb} src=${scanThumb(project, scan)} scan=${true} /><span class="grow ellipsis">${scan.name}</span>
        </button>
        <span class="row-actions">
          <${IconButton} icon="sparkles" label=${t('Create a parametric model from {name}', { name: scan.name })} disabled=${!canCreate} onClick=${() => attempt(() => modelFromScan(scan.name))} />
          <${IconButton} icon="pencil" label=${t('Rename {name}', { name: scan.name })} disabled=${locked} onClick=${() => setRenaming({ name: scan.name, scan: true })} />
          <${IconButton} icon="trash" label=${t('Delete {name}', { name: scan.name })} disabled=${locked} onClick=${() => removeScan(scan.name)} />
        </span>
      </div>`))}
    ${dragging && html`<p class="list-empty">${t('Drop it here')}</p>`}
  </div>`;
}

// A prompt about one spot on the model, opened with the right mouse button
// where the spot is. Enter sends, Escape closes.
function SpotPrompt({ spot, project, model, viewer, stage, onClose }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const box = useRef(null);
  const ref = useDismiss(true, onClose);
  useEffect(() => box.current?.focus(), []);
  const send = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    const { x, y, ...pick } = spot;
    // What the person sees there goes along, so "this" means the same to Claude.
    let view = null;
    try { view = viewer?.snapshot(pick.point); } catch { /* the words and the point are enough */ }
    const files = view ? [{ name: 'widok-wskazanego-miejsca.jpg', type: 'image/jpeg', data: view }] : undefined;
    setState({ tab: 'chat', listing: false, peek: null }); // the answer arrives in the conversation
    const ok = await attempt(() => api(`/projects/${project.id}/prompt`, { file: project.file, text: text.trim(), model, pick, files }));
    if (ok) onClose({ keepMarker: true });
    else setBusy(false);
  };
  // Beside the spot, kept inside the stage.
  const left = Math.min(spot.x + 34, (stage?.clientWidth ?? 9999) - 336);
  const top = Math.min(spot.y - 18, (stage?.clientHeight ?? 9999) - 70);
  return html`<div ref=${ref} class="spot-prompt" style=${`left:${Math.max(8, left)}px;top:${Math.max(56, top)}px`} role="dialog" aria-label=${t('Prompt for the picked spot')}>
    <div class="spot-row">
      <input ref=${box} value=${text} placeholder=${project.turn ? t('Wait until I finish') : t('What should be done here?')} disabled=${!!project.turn || busy}
        onInput=${(e) => setText(e.target.value)}
        onKeyDown=${(e) => {
          if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); send(); }
          if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
        }} />
      <button type="button" class="send" aria-label=${t('Send')} disabled=${!text.trim() || busy || !!project.turn} onClick=${send}><${Icon} name="up" /></button>
    </div>
  </div>`;
}

function Stage({ s }) {
  const { project, peek, options } = s;
  const stage = useRef(null);
  const host = useRef(null);
  const viewer = useRef(null);
  const shownFor = useRef(null);
  const [width, setWidth] = useState(1200);

  useEffect(() => {
    viewer.current = createViewer(host.current);
    viewer.current.setTheme(state.theme);
    const observer = new ResizeObserver(() => setWidth(stage.current.clientWidth));
    observer.observe(stage.current);
    return () => {
      observer.disconnect();
      viewer.current.dispose();
    };
  }, []);

  // An STL picked in the list takes the stage: the model steps aside.
  const scanOpen = project?.scans.some((scan) => scan.name === s.scanOpen) ? s.scanOpen : null;
  const meta = scanOpen || s.unpicked ? null : peek ? peek.meta : project?.good ?? null;
  const metaKey = meta?.key ?? null;
  useEffect(() => {
    if (!viewer.current) return;
    if (!project || !meta) {
      if (!peek || scanOpen || s.unpicked) viewer.current.show(null);
      return;
    }
    const id = project.id;
    const fresh = shownFor.current !== `${id}/${project.file}`;
    shownFor.current = `${id}/${project.file}`;
    viewer.current.show(meta, (file) => `/api/projects/${id}/mesh/${meta.key}/${file}`, { fitView: fresh })
      .catch((err) => toast(err.message, { error: true }));
  }, [project?.id, project?.file, metaKey]);

  // The scans drawn with the model: a list that changes only when it has to.
  const scans = !project ? [] : project.scans.filter((scan) => (scanOpen ? scan.name === scanOpen : !s.unpicked && options.scans && scanVisible(scan.name, meta)));
  const scansKey = scans.map((scan) => `${scan.name}:${scan.updatedAt}`).join('|');
  useEffect(() => {
    if (!viewer.current) return;
    const id = project?.id;
    viewer.current.setScans(scans.map((scan) => ({ name: scan.name, url: `${scanPath(id, scan.name)}?v=${Math.round(scan.updatedAt)}` })))
      .catch((err) => toast(err.message, { error: true }));
  }, [project?.id, scansKey]);

  useEffect(() => { viewer.current?.setTheme(s.theme); }, [s.theme]);

  // The labels in the view hold numbers written in the language's way.
  useEffect(() => { viewer.current?.relabel(); }, [s.lang]);

  useEffect(() => {
    viewer.current?.setOptions({ dims: options.dims, refs: options.refs, grid: options.grid });
  }, [options.dims, options.refs, options.grid]);

  // The code panel takes the left of the stage; the model is centred in what is left.
  const codeOpen = !!(project?.file && options.code && !s.unpicked && !scanOpen);
  const left = codeOpen ? Math.round(Math.min(600, width * 0.52)) + 20 : 0;
  const right = 0;
  useEffect(() => {
    viewer.current?.setInsets({ top: 48, right, bottom: 56, left });
  }, [left, right]);

  // Right click on the model: a prompt about that spot.
  const onContextMenu = (event) => {
    if (!project?.file || peek || scanOpen || s.unpicked || !viewer.current) return;
    const spot = viewer.current.pick(event.clientX, event.clientY);
    if (!spot) return;
    event.preventDefault();
    const rect = stage.current.getBoundingClientRect();
    viewer.current.setMarker(spot);
    setState({ spot: { ...spot, x: event.clientX - rect.left, y: event.clientY - rect.top } });
  };
  const closeSpot = ({ keepMarker = false } = {}) => {
    if (!keepMarker) viewer.current?.setMarker(null);
    setState({ spot: null });
  };
  // The marker stays while the turn about it runs.
  useEffect(() => {
    if (!project?.turn && !s.spot) viewer.current?.setMarker(null);
  }, [!!project?.turn]);

  const toggle = (name) => () => setState((st) => ({ options: { ...st.options, [name]: !st.options[name] } }));
  const turn = project?.turn;
  const failing = project?.build && !project.build.ok && !project.build.empty && !peek && !s.unpicked && !scanOpen;
  const hasRefs = meta?.parts?.some((part) => part.kind !== 'show');
  const hasScans = !scanOpen && (meta?.scans ?? []).some((scan) => project.scans.some((file) => file.name === scan.name));
  const something = !!meta?.ok || scans.length > 0; // there is a shape on the stage
  const stl = project?.file ? `${project.file.replace(/\.py$/, '')}.stl` : null;
  const shown = !!project?.good && !peek && !scanOpen && !s.unpicked; // the model on the stage has files to hand out
  const act = (action, body, done) => () => attempt(() => api(filePath(action), body), done);
  const status = peek ? null
    : turn ? phaseText(turn.phase)
    : s.uploading ? (s.uploading > 1 ? t('Loading files ({count} left)…', { count: s.uploading }) : t('Loading the file…'))
    : s.opening ? t('Opening {name}…', { name: s.opening })
    : s.building ? phaseText('building') : null;

  return html`<main class="stage" ref=${stage} style=${`--left:${left}px;--right:${right}px`}>
    <div class=${`viewer ${!project || s.opening || s.building ? 'is-loading' : turn && turn.file === project.file && !peek && !scanOpen ? 'is-working' : ''}`} ref=${host} onContextMenu=${onContextMenu} />
    ${s.spot && project && html`<${SpotPrompt} key=${s.spot.point.join()} spot=${s.spot} project=${project} model=${s.model} viewer=${viewer.current} stage=${stage.current} onClose=${closeSpot} />`}

    ${project && html`<div class="overlay-card stage-tr">
      ${something && html`
        <${IconButton} icon="viewIso" label=${t('Isometric view')} onClick=${() => viewer.current.view('iso')} />
        <${IconButton} icon="viewTop" label=${t('View from the top')} onClick=${() => viewer.current.view('top')} />
        <${IconButton} icon="viewFront" label=${t('View from the front')} onClick=${() => viewer.current.view('front')} />
        <${IconButton} icon="viewSide" label=${t('View from the side')} onClick=${() => viewer.current.view('right')} />
        <span class="sep" />
        ${shown && html`
          <a class="btn btn-ghost btn-icon btn-sm" href=${`/api${filePath('download/stl')}`} title=${t('Download STL')} aria-label=${t('Download STL')}><${Icon} name="download" /></a>
          <${IconButton} icon="folder" label=${t('Show {name} in the folder', { name: stl })} onClick=${() => attempt(() => api(filePath('reveal'), {}))} />
          <span class="sep" />`}
        <${IconButton} icon="ruler" label=${t('Dimensions and hole diameters')} on=${options.dims} onClick=${toggle('dims')} />
        ${hasScans && html`<${IconButton} icon="scan" label=${t('The STL this model recreates')} on=${options.scans} onClick=${toggle('scans')} />`}
        ${hasRefs && html`<${IconButton} icon="layers" label=${t('Helper parts (screws, mating parts)')} on=${options.refs} onClick=${toggle('refs')} />`}
        <span class="sep" />`}
      <${ThemeToggle} theme=${s.theme} small=${true} />
    </div>`}

    ${peek ? html`<div class="status-pill is-peek" role="status">
      ${!peek.meta && html`<span class="spinner" />`}
      <span>${t('Preview of the version from {time}', { time: stamp(peek.time, s.now) })}</span>
      <button type="button" class="btn btn-sm btn-ghost" onClick=${() => setState({ peek: null })}>${t('Back to the newest')}</button>
    </div>` : status && html`<div class="status-pill" role="status"><span class="spinner" /><span>${status}</span></div>`}

    ${project && meta?.ok && html`<${Summary} meta=${meta} />`}
    ${codeOpen && html`<${CodeView} project=${project} peek=${peek} />`}

    ${failing && html`<div class="banner" role="alert"><${Icon} name="alert" /><div>
      <strong>${t('The model does not build.')}</strong> ${project.good ? t('Showing the last version that built.') : ''}
      <div class="mono">${project.build.error?.line ? `${t('line {line}', { line: project.build.error.line })}: ` : ''}${project.build.error?.type}: ${project.build.error?.message}</div>
    </div></div>`}

    ${project && !something && !failing && !turn && !peek && html`<div class="stage-empty"><div class="card">
      <svg class="big" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" dangerouslySetInnerHTML=${{ __html: ICONS.box }} />
      <h2>${!project.file ? t('This project has no model yet') : s.unpicked ? t('No model chosen') : t('This model is still empty')}</h2>
      <p>${project.file && s.unpicked ? t('Choose a model on the left to see it and work on it.') : project.file
        ? t('Describe on the left what should be made. You can also drop an STL or a model file (.py) onto the window.')
        : t('Start a model on the left, or drop an STL or a model file (.py) onto the window.')}</p>
    </div></div>`}
  </main>`;
}

// --- dashboard: every project, in the middle of the page ----------------------

function showHome() {
  events?.close();
  events = null;
  fileLoad += 1;
  setState({ home: true, projectId: null, project: null, opening: null, peek: null, building: false, spot: null, scanOpen: null });
  history.replaceState(null, '', location.pathname);
  refreshProjects();
}

function ProjectRow({ project, now }) {
  const remove = () => setState({
    dialog: {
      kind: 'confirm',
      title: t('Delete the project {name}?', { name: project.id }),
      lead: t('It disappears from this list, but nothing is destroyed: its folder moves to .data/trash in the app folder.'),
      label: t('Delete project'),
      action: async () => {
        await api(`/projects/${project.id}/delete`, {});
        await refreshProjects();
      },
    },
  });
  const [naming, setNaming] = useState(false);
  const contents = [
    project.models + project.scans ? plural(project.models + project.scans, 'model', 'models') : t('no model'),
  ].filter(Boolean).join(', ');
  // While its name is edited the row is not a button: the field stands where the name was.
  if (naming) {
    return html`<li class="project-row is-form">
      <div class="project-row-open">
        <${Icon} name="project" />
        <${NameEdit} value=${project.id} hint=${t('Its folder will be renamed too.')} onSave=${(name) => renameProject(project.id, name)} onClose=${() => setNaming(false)} />
        <span class="project-row-meta">${contents}</span>
      </div>
    </li>`;
  }
  return html`<li class="project-row">
    <button type="button" class="project-row-open" onClick=${() => openProject(project.id)}>
      <${Icon} name="project" />
      <span class="project-row-text">
        <span class="project-row-name ellipsis">${project.id}</span>
        <span class="project-row-when">${when(project.updatedAt, now)}</span>
      </span>
      <span class="project-row-meta">${contents}</span>
    </button>
    <span class="project-row-actions">
      <${IconButton} icon="pencil" label=${t('Rename the project {name}', { name: project.id })} onClick=${() => setNaming(true)} />
      <${IconButton} icon="copy" label=${t('Duplicate the project {name}', { name: project.id })} onClick=${() => setState({ dialog: { kind: 'duplicate', projectId: project.id } })} />
      <${IconButton} icon="trash" label=${t('Delete the project {name}', { name: project.id })} onClick=${remove} />
    </span>
  </li>`;
}

function Home({ s }) {
  const projects = [...s.info.projects].sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
  return html`<div class="home">
    <header class="home-head">
      <button type="button" class="logo" title=${t('All projects')} aria-label=${t('All projects')} onClick=${showHome}><${Icon} name="box" /></button>
      <span class="home-brand">${document.title}</span>
      <span class="grow" />
      <${LanguagePicker} lang=${s.lang} />
      <${ThemeToggle} theme=${s.theme} />
    </header>
    <main class="home-main">
      <div class="home-title">
        <h1>${t('Projects')}</h1>
        <span class="grow" />
        <button type="button" class="btn btn-primary" onClick=${() => attempt(() => createProject())}><${Icon} name="plus" />${t('New project')}</button>
      </div>
      ${projects.length === 0
        ? html`<div class="home-empty">
            <p>${t('There are no projects yet. A project is a folder with models and STL files, with its own history of changes. Create one, or drop a model or an STL file onto the window.')}</p>
          </div>`
        : html`<ul class="project-list">
            ${projects.map((project) => html`<${ProjectRow} key=${project.id} project=${project} now=${s.now} />`)}
          </ul>`}
      <p class="home-foot">${tx('Projects are kept in {path}.', { path: mono(s.info.root) })}</p>
    </main>
  </div>`;
}

// --- app ---------------------------------------------------------------------

function App() {
  const s = useStore();
  const [draft, setDraft] = useState('');
  const [files, setFiles] = useState([]);
  const filesRef = useRef(files);
  filesRef.current = files;

  useEffect(() => {
    setState({ theme: currentTheme() });
    const follow = () => { if (!storedTheme()) setState({ theme: currentTheme() }); };
    darkQuery.addEventListener('change', follow);
    boot();
    const clock = setInterval(() => setState({ now: Date.now() }), 30_000);
    return () => {
      darkQuery.removeEventListener('change', follow);
      clearInterval(clock);
    };
  }, []);

  // Cmd/Ctrl+Z takes back the last change to the model; with Shift it brings it back. A text field with
  // something typed in it keeps the key for its own undo.
  useEffect(() => {
    const onKey = (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== 'z') return;
      const field = event.target.closest?.('input, textarea, [contenteditable="true"]');
      if (field && (field.value ?? field.textContent ?? '') !== '') return;
      const { project } = state;
      if (state.dialog || state.home || state.unpicked || state.scanOpen || !project?.file) return;
      event.preventDefault();
      // With Shift the same key brings back what was just undone.
      if (project.turn || !(event.shiftKey ? project.redoTo : project.undoTo)) return;
      attempt(() => api(filePath(event.shiftKey ? 'redo' : 'undo'), {}), event.shiftKey ? t('Redone') : t('Undone'));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Files dropped anywhere on the page: a mesh becomes a scan of the project
  // (and is named in the prompt, beside a conversation), anything else is
  // context for the next prompt. With no project open, they
  // start a new one.
  useEffect(() => {
    let depth = 0;
    const hasFiles = (event) => [...(event.dataTransfer?.types ?? [])].includes('Files');
    const enter = (event) => { if (hasFiles(event)) { depth += 1; setState({ dragging: true }); } };
    const leave = (event) => { if (hasFiles(event)) { depth = Math.max(0, depth - 1); if (!depth) setState({ dragging: false }); } };
    const over = (event) => { if (hasFiles(event)) event.preventDefault(); };
    const drop = (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth = 0;
      setState({ dragging: false });
      const dropped = [...event.dataTransfer.files];
      if (state.dialog) return;
      if (!state.project || state.home) {
        const usable = dropped.filter((file) => isModelFile(file) || isScanFile(file));
        if (usable.length && (!state.projectId || state.home)) attempt(() => createProject(usable));
        return;
      }
      const meshes = dropped.filter(isScanFile);
      // On the list of models a dropped model joins the project; beside a conversation it is context for the prompt.
      const listing = state.listing || state.unpicked || !state.project.file;
      const models = listing ? dropped.filter(isModelFile) : [];
      const rest = dropped.filter((file) => !isScanFile(file) && !models.includes(file));
      if (models.length) {
        if (state.project.turn) toast(t('Wait until I finish'), { error: true });
        else importModels(models);
      }
      if (meshes.length) {
        if (state.project.turn) toast(t('Wait until I finish'), { error: true });
        // Beside a conversation the STL is also named in its prompt.
        else addScans(meshes).then((names) => { if (!listing && !state.scanOpen) nameScans(names, setDraft); });
      }
      if (rest.length) addFiles(rest, filesRef.current, setFiles);
    };
    window.addEventListener('dragenter', enter);
    window.addEventListener('dragleave', leave);
    window.addEventListener('dragover', over);
    window.addEventListener('drop', drop);
    return () => {
      window.removeEventListener('dragenter', enter);
      window.removeEventListener('dragleave', leave);
      window.removeEventListener('dragover', over);
      window.removeEventListener('drop', drop);
    };
  }, []);

  // A photo from the camera joins the files of the next prompt. One of a sketch
  // brings its request along, ready to send or to add the dimensions to.
  const addPhoto = (photo, sketch) => {
    const taken = filesRef.current;
    const base = sketch ? t('sketch') : t('photo');
    let name = `${base}.jpg`;
    for (let n = 2; taken.some((file) => file.name === name); n += 1) name = `${base}-${n}.jpg`;
    setFiles([...taken, { name, ...photo }]);
    if (sketch) setDraft((was) => (was.trim() ? was : t('Make a model from the sketch in the photo')));
    requestAnimationFrame(() => {
      const box = document.querySelector('.composer textarea');
      box?.focus();
      box?.setSelectionRange(box.value.length, box.value.length);
    });
  };

  if (s.boot === 'loading') return html`<div class="screen"><div class="working"><span class="spinner" />${t('Starting up…')}</div></div>`;
  if (s.boot !== 'ready') {
    return html`<div class="screen"><div class="card">
      <h1>${s.boot === 'denied' ? t('This tab has no access') : t('The app is not responding')}</h1>
      <p>${s.boot === 'denied' ? t('Open the editor from the address printed in the terminal, so that the browser gets the access key. In the app folder:') : t('Restart it. In the app folder:')}</p>
      <p><code>${START_COMMAND}</code></p>
    </div></div>`;
  }
  return html`<div class="app">
    <${Side} s=${s} draft=${draft} setDraft=${setDraft} files=${files} setFiles=${setFiles} />
    <${Stage} s=${s} />
    ${s.home && html`<${Home} s=${s} />`}
    ${s.dialog?.kind === 'duplicate' && html`<${DuplicateDialog} projectId=${s.dialog.projectId} />`}
    ${s.dialog?.kind === 'confirm' && html`<${ConfirmDialog} ...${s.dialog} />`}
    ${s.dialog?.kind === 'camera' && html`<${CameraDialog} onUse=${(photo) => addPhoto(photo, s.dialog.sketch)} />`}
    <div class="toasts" role="status" aria-live="polite">
      ${s.toasts.map((item) => html`<div class=${`toast ${item.error ? 'is-error' : ''}`} key=${item.id}>${item.text}</div>`)}
    </div>
  </div>`;
}

// The language first, so that nothing is drawn in the wrong one.
setLanguage(preferredLanguage()).then((lang) => {
  state = { ...state, lang };
  render(html`<${App} />`, document.getElementById('app'));
});
