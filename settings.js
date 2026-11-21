// The score settings panel, in groups that fold away. Each setting has a box for an exact number and
// a slider. The sliders go in steps of 1, 1.5, 2, 3, 5 and 7 times a power of ten, so one slider covers
// small and huge numbers. A group's heading says how many of its settings differ from the defaults.
const GROUPS = [
  { id: "feed", title: "Feed", open: true },
  { id: "opened", title: "Opened and next up", open: true },
  { id: "tags", title: "Tags", open: true },
  { id: "kind", title: "Kind, length and age" },
  { id: "stop", title: "Stop time" },
  { id: "tasks", title: "Tasks" },
  { id: "similarity", title: "Similarity" },
];
const SETTINGS = [
  { group: "feed", key: "post_limit", label: "Post limit", before: "show at most", after: "posts", blankIsMax: true,
    hint: "0 empties the feed; 1 shows one post at a time. Leave empty for no limit." },
  { group: "feed", key: "enrich_count", label: "Fetch details", after: "posts on load",
    hint: "Fetch missing lengths and dates for this many top posts. Adds no points." },

  { group: "opened", key: "seen_bonus", label: "Opened", after: "points",
    hint: "For posts you've opened. Tagging doesn't count as opening." },
  { group: "opened", key: "next_up_bonus", label: "Next up", after: "points",
    hint: "For the post after the one you last opened in a subscription." },
  { group: "opened", key: "new_sub_bonus", label: "New subscription", after: "points",
    hint: "For posts from a subscription you've never opened anything from." },
  { group: "opened", key: "series_penalty", label: "Unstarted series", after: "points",
    hint: "For a later part of a series when you haven't opened an earlier one." },

  { group: "tags", key: "green_points", label: "Show again soon (green)", after: "points" },
  { group: "tags", key: "white_points", label: "Maybe later (white)", after: "points" },
  { group: "tags", key: "red_points", label: "Don't show again (red)", after: "points" },

  { group: "kind", key: "video_bonus", label: "Video", after: "points",
    hint: "YouTube, Jellyfin, NRK TV and other video pages." },
  { group: "kind", key: "youtube_shorts_bonus", label: "YouTube Shorts", after: "points",
    hint: "Added on top of Video." },
  { group: "kind", key: "image_bonus", label: "Image", after: "points",
    hint: "Links straight to a picture." },
  { group: "kind", key: "article_bonus", label: "Article", after: "points",
    hint: "Everything else." },
  { group: "kind", key: "length_per_minute", label: "Length", after: "points per minute from",
    extra: { key: "length_target_minutes", after: "minutes" },
    hint: "Below 0 favors posts near the target length. Unknown lengths get none." },
  { group: "kind", key: "age_per_year", label: "Age", after: "points per year",
    hint: "Below 0 favors newer posts." },

  { group: "stop", key: "event_lead_minutes", label: "Event lead time", after: "minutes before an event",
    hint: "Each calendar event is a stop time this long before it starts." },
  { group: "stop", key: "past_stop_per_minute", label: "Past the stop time", after: "points per minute",
    hint: "For each minute a post would run past the stop time. The post you're viewing is exempt." },

  { group: "tasks", key: "task_deadline_bonus", label: "Deadline", after: "points when due",
    hint: "Ramps up over the last 30 days before a task's deadline." },
  { group: "tasks", key: "task_priority_bonus", label: "Priority", after: "points at priority 1",
    hint: "A ninth less per level, down to priority 9." },
  { group: "tasks", key: "task_bonus", label: "Every task", after: "points" },

  { group: "similarity", key: "similar_penalty", label: "Like something opened", after: "points, fading over",
    extra: { key: "similar_hours", after: "hours" },
    hint: "Per 100% similarity to each post you opened recently." },
  { group: "similarity", key: "above_similarity_penalty", label: "Like posts above", after: "points",
    hint: "Per 100% similarity to each post above it, to mix up the feed." },
  { group: "similarity", key: "similar_base_percent", label: "Same subscription", after: "% alike",
    hint: "Where similarity starts for posts from one subscription; the weights below fill the rest. Other subscriptions never match." },
  { group: "similarity", key: "similar_series_percent", label: "Same series", after: "% alike",
    hint: "Used instead for posts in one series, when higher." },
  { group: "similarity", key: "similar_days", label: "Publish window", before: "alike within", after: "days" },
  { group: "similarity", key: "similar_publish_weight", label: "Publish time weight", after: "" },
  { group: "similarity", key: "similar_length_weight", label: "Length weight", after: "",
    hint: "Relative to the publish time weight. Unknown lengths are left out." },
];
// Every setting, including the second box some have, such as Similar's hours
const KEYS = SETTINGS.flatMap(({ key, extra }) => (extra ? [key, extra.key] : [key]));
let limits = {};  // each term's {min, max, integer}, from the server
let saved = {};  // as saved on the server
let draft = {};  // as shown in the panel
let defaults = {};

// Slider positions: 0, then 1, 1.5, 2, 3, 5, 7 times each power of ten from 10^lo up to 10^hi,
// mirrored below 0 for terms that may be negative.
function steps(lo, hi, signed) {
  const up = [];
  for (let e = lo; e < hi; e++) for (const n of [1, 1.5, 2, 3, 5, 7]) up.push(Number((n * 10 ** e).toPrecision(2)));
  up.push(10 ** hi);
  return signed ? [...up.map((v) => -v).reverse(), 0, ...up] : [0, ...up];
}
const SIGNED_STEPS = steps(0, 3, true);  // ±1 to ±1000 points
const BONUS_STEPS = steps(0, 3, false);  // 1 to 1000 points
// Whole numbers of posts, ending in the maximum: no limit
const POST_LIMIT_STEPS = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000, 10_000_000];

// The slider position closest to a typed number, on the slider's log scale.
function nearestStep(list, value) {
  const f = (x) => (x === 0 ? 0 : Math.sign(x) * (Math.log10(Math.abs(x)) + 4));
  let best = 0;
  list.forEach((v, i) => { if (Math.abs(f(v) - f(value)) < Math.abs(f(list[best]) - f(value))) best = i; });
  return best;
}

const fmt = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 3 }).replaceAll(",", " ").replace("-", "−");
const sameTerms = (a, b) => KEYS.every((key) => a[key] === b[key]);
const inRange = (key, v) => Number.isFinite(v) && v >= limits[key].min && v <= limits[key].max && (!limits[key].integer || Number.isInteger(v));
const controls = {};
const groupEls = {};

// The group headings' fold state stays as you left it, in this browser.
function groupOpen(group) {
  try {
    const stored = JSON.parse(localStorage.getItem("settingsGroups") || "{}");
    if (group.id in stored) return stored[group.id];
  } catch {}
  return !!group.open;
}
function rememberGroup(id, open) {
  try {
    const stored = JSON.parse(localStorage.getItem("settingsGroups") || "{}");
    localStorage.setItem("settingsGroups", JSON.stringify({ ...stored, [id]: open }));
  } catch {}
}

// A number box's value: a blank post limit means no limit (the maximum).
function boxValue(box, key, blankIsMax) {
  if (box.value.trim() === "") return blankIsMax ? limits[key].max : NaN;
  return Number(box.value);
}
function showBox(box, key, value, blankIsMax) {
  if (document.activeElement === box) return;
  box.value = blankIsMax && value === limits[key].max ? "" : value;
}

function settingRow({ key, label, before, after, extra, hint, blankIsMax }) {
  const { min, max } = limits[key];
  const list = key === "post_limit" ? POST_LIMIT_STEPS
    : max <= 100 ? Array.from({ length: max + 1 }, (_, i) => i) : min < 0 ? SIGNED_STEPS : BONUS_STEPS;
  const num = el("input", { type: "number", id: `w-${key}`, step: "any", inputmode: "decimal", min, max,
    ...(blankIsMax ? { placeholder: "none" } : { required: "" }) });
  const slider = el("input", { type: "range", min: 0, max: list.length - 1, step: 1, "aria-label": `${label} slider` });
  const def = el("span", { class: "default" });
  controls[key] = { num, slider, list, def, blankIsMax };
  slider.addEventListener("input", () => {
    showBox(num, key, list[slider.value], blankIsMax);
    change(key, list[slider.value]);
  });
  num.addEventListener("input", () => {
    const v = boxValue(num, key, blankIsMax);
    if (!inRange(key, v)) return;
    slider.value = nearestStep(list, v);
    change(key, v);
  });
  slider.addEventListener("change", save);  // when you let go of the slider
  num.addEventListener("change", save);  // when you press Enter or leave the box
  // A second box, such as how many hours count as recent, without a slider
  let second = [];
  if (extra) {
    const box = el("input", { type: "number", id: `w-${extra.key}`, step: "any", inputmode: "decimal", required: "",
      min: limits[extra.key].min, max: limits[extra.key].max, "aria-label": `${label} ${extra.after}` });
    controls[extra.key] = { num: box };
    box.addEventListener("input", () => {
      const v = boxValue(box, extra.key, false);
      if (inRange(extra.key, v)) change(extra.key, v);
    });
    box.addEventListener("change", save);
    second = [box, extra.after];
  }
  return el("div", { class: "setting" },
    el("label", { for: `w-${key}` }, label),
    el("span", { class: "num" }, before || "", num, after || "", ...second),
    slider,
    el("p", { class: "hint" }, hint ? `${hint} ` : "", def));
}

function buildSettings() {
  document.getElementById("settings").replaceChildren(...GROUPS.map((group) => {
    const count = el("span", { class: "changed" });
    const details = el("details", { class: "group" },
      el("summary", {}, group.title, count),
      ...SETTINGS.filter((s) => s.group === group.id).map(settingRow));
    details.open = groupOpen(group);
    details.addEventListener("toggle", () => rememberGroup(group.id, details.open));
    groupEls[group.id] = { details, count };
    return details;
  }));
}

function showSettings(w) {
  for (const { key, extra } of SETTINGS) {
    const { num, slider, list, blankIsMax } = controls[key];
    showBox(num, key, w[key], blankIsMax);
    slider.value = nearestStep(list, w[key]);
    if (extra) showBox(controls[extra.key].num, extra.key, w[extra.key], false);
  }
  showChanged();
}

// Each group's heading counts its settings that differ from the defaults, so you can see where
// you've changed something without opening every group. The dots on the cards show the tag points.
function showChanged() {
  for (const group of GROUPS) {
    const n = SETTINGS.filter((s) => s.group === group.id
      && [s.key, s.extra?.key].some((k) => k && draft[k] !== defaults[k])).length;
    groupEls[group.id].count.textContent = n ? `${n} changed` : "";
  }
  if (typeof relabelDots === "function") relabelDots();
}

function panelSay(text, isError = false) {
  const status = document.getElementById("panel-status");
  status.textContent = text;
  status.className = isError ? "error" : "";
}

// A setting moved: show the feed in its new order right away, from the top.
function change(key, value) {
  draft = { ...draft, [key]: value };
  showChanged();
  panelSay("");
  load({ reorder: true });
}

// Saves run one after another, so the last change wins even when two start together, such as a
// number box saving as you click Reset.
let saving = Promise.resolve();
function save() {
  saving = saving.then(saveNow);
  return saving;
}

async function saveNow() {
  const invalid = KEYS.find((key) => !inRange(key, draft[key]));
  if (invalid) {
    const { label } = SETTINGS.find((s) => s.key === invalid || s.extra?.key === invalid);
    const { min, max, integer } = limits[invalid];
    return panelSay(`${label} must be a ${integer ? "whole " : ""}number from ${fmt(min)} to ${fmt(max)}.`, true);
  }
  if (sameTerms(draft, saved)) return;
  const { ok, data } = await api("/api/settings", { terms: draft });
  if (!ok) return panelSay(`Couldn't save: ${data.error || "unknown error"}`, true);
  saved = data.terms;
  panelSay("Saved.");
}

let rankerShown = null;
function rankerNote(ranker) {
  if (ranker === rankerShown) return;
  rankerShown = ranker;
  if (ranker !== "score") panelSay(`The feed is ranked by ${ranker}, which doesn't use the score terms.`);
}

async function loadSettings() {
  const { ok, data } = await api("/api/settings");
  if (!ok) throw new Error(data.error);
  saved = data.terms;
  draft = { ...saved };
  defaults = data.defaults;
  limits = data.limits;
  buildSettings();
  showStopTimer(data.stop_at);
  for (const { key, extra, blankIsMax } of SETTINGS) {
    const value = blankIsMax && defaults[key] === limits[key].max ? "no limit" : fmt(defaults[key]);
    const second = extra ? ` and ${fmt(defaults[extra.key])} ${extra.after}` : "";
    controls[key].def.textContent = `Default ${value}${second}.`;
  }
  showSettings(draft);
}

document.getElementById("reset").addEventListener("click", async () => {
  if (sameTerms(draft, defaults)) return panelSay("These are the defaults.");
  draft = { ...defaults };
  showSettings(draft);
  await save();
  load({ reorder: true });
});

// The panel stays open or closed across reloads, in this browser.
function setPanel(open) {
  document.getElementById("panel").hidden = !open;
  document.body.classList.toggle("panel-open", open);
  document.getElementById("panel-toggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("scorePanel", open ? "open" : "closed"); } catch {}
}
document.getElementById("panel-toggle").addEventListener("click", () => setPanel(document.getElementById("panel").hidden));
document.getElementById("panel-close").addEventListener("click", () => setPanel(false));
let panelOpen = false;
try { panelOpen = localStorage.getItem("scorePanel") === "open"; } catch {}
setPanel(panelOpen);

