// The score settings panel. Each setting has a slider and a box for an exact number. The sliders go
// in steps of 1, 1.5, 2, 3, 5 and 7 times a power of ten, so one slider covers small and huge numbers.
// A setting with a term only changes how that term's points are worked out, such as Similar's days,
// so it's shown and hidden with it.
const SETTINGS = [
  { key: "enrich_count", label: "Update incomplete posts", before: "", after: "posts on page load",
    hint: "Fetch missing runtime or exact publication time for this many highest-ranked incomplete posts when you open the site. 0 turns this off. This does not add score points." },
  { key: "next_up_bonus", label: "Next up", before: "", after: "points",
    hint: "Next up is the post after the one you opened most recently in a subscription: the next part of its series, or else the next published. Not for subscriptions tagged “latest”." },
  { key: "seen_bonus", label: "Seen", before: "", after: "points",
    hint: "For items you've opened. Below 0: they go under things you haven't opened. Tagging an item doesn't count as opening it." },
  { key: "new_sub_bonus", label: "New subscription", before: "", after: "points",
    hint: "For posts from a subscription you've never opened anything from, such as one you just subscribed to." },
  { key: "series_penalty", label: "Series", before: "", after: "points",
    hint: "Added to a later part of a series when you haven't opened anything before it. YouTube series use imported channel playlists in playlist order. Other posts follow another from their subscription when their titles have the same numbers but one, 1 higher, such as “part 2” after “part 1”; Jellyfin and NRK TV episodes go in episode order. Not for subscriptions tagged “latest”." },
  { key: "above_similarity_penalty", label: "Similar posts above", before: "", after: "points per 100% similarity",
    hint: "Adds signed points for similarity to each post already above it, using the publish-time, content-length and title similarity weights below. Two 50% matches add up to the full configured points. Negative points penalize similarity. Different subscriptions do not match. No viewing-time fade." },
  { key: "event_lead_minutes", label: "Event lead time", before: "stop watching", after: "minutes before an event",
    hint: "Every event that hasn't ended acts as a stop time this many minutes before it starts: while it's the earliest stop in effect, the feed shows only posts you can finish before then, so you can get to the event in time. 0 stops exactly when the event starts. The stop timer at the top can set an earlier time; the earliest stop wins. Events aren't feed items, so they never appear as cards." },
  { key: "task_deadline_bonus", label: "Task urgency", before: "", after: "points at deadline",
    hint: "Maximum points for a task due now. The bonus ramps from 0 to this amount during the final 30 days before its deadline. Tasks in the same calendar can also get the similar-posts-above penalty." },
  { key: "task_bonus", label: "Task", before: "", after: "points",
    hint: "Fixed points added to every task, independent of its deadline. Below 0: a penalty. Default: 0." },
  { key: "task_priority_bonus", label: "Task priority", before: "", after: "points at priority 1",
    hint: "Points for a task by its calendar priority: all of them at priority 1 (highest), scaling down by a ninth per level to a ninth of them at priority 9 (lowest). Many apps use 1 for high, 5 for medium and 9 for low. Tasks without a priority get none. Below 0: a penalty. Default: 0." },
  { key: "video_bonus", label: "Video", before: "", after: "points",
    hint: "For YouTube, Jellyfin and NRK TV videos. Below 0: a penalty." },
  { key: "youtube_shorts_bonus", label: "YouTube Shorts", before: "", after: "points",
    hint: "Extra points for YouTube Shorts links, added alongside Video points. Below 0: a penalty." },
  { key: "image_bonus", label: "Image", before: "", after: "points",
    hint: "For links straight to a picture. Below 0: a penalty." },
  { key: "article_bonus", label: "Article", before: "", after: "points",
    hint: "For everything else: articles, blog posts and other pages. Below 0: a penalty." },
  { key: "length_per_minute", label: "Content length", before: "", after: "points per minute away from",
    extra: { key: "length_target_minutes", type: "number", after: "target minutes" },
    hint: "Weight × absolute distance from the target runtime or reading time at 200 words per minute. Negative favors content near the target; positive favors content farther away. Unknown lengths have no adjustment. Weight and target default to 0." },
  { key: "age_per_year", label: "Age", before: "", after: "points per year",
    hint: "Fractional 365-day years since publication for posts, or since addition for links, updated in whole hours. Negative favors newer items; positive favors older items. Future dates count as age 0. Default: 0." },
  { key: "green_points", label: "Show again soon (green)", before: "", after: "points",
    hint: "For items you tagged with the green dot. Only adds these points; it doesn't count as opening the item. Default: +10." },
  { key: "white_points", label: "Maybe later (white)", before: "", after: "points",
    hint: "For items you tagged with the white dot. Only adds these points; it doesn't count as opening the item. Default: −50." },
  { key: "red_points", label: "Don't show again (red)", before: "", after: "points",
    hint: "For items you tagged with the red dot. Only adds these points; it doesn't count as opening the item. Default: −1000." },
  { key: "similar_penalty", label: "Similar", before: "", after: "points at 100% alike, within",
    extra: { key: "similar_hours", type: "number", after: "hours" },
    hint: "Added to items alike to something you opened, in proportion: 50% alike adds half the configured points. Negative points penalize similarity. The adjustment fades linearly to zero over this many hours, updated in whole hours. Only the strongest remaining penalty counts. 0 points or 0 hours: off." },
  { key: "similar_base_percent", term: "similarity", label: "Subscription base similarity", before: "", after: "%",
    hint: "Same-subscription posts start at this similarity. Other components fill the remainder: 50% base + 50% × 60% measured = 80%. Different subscriptions do not match. 0 restores similarity from components alone." },
  { key: "similar_series_percent", term: "similarity", label: "Series base similarity", before: "", after: "%",
    hint: "Posts in the same series of a subscription (a YouTube playlist, a Jellyfin or NRK TV show, or numbered titles) start at this similarity instead, if it's higher than the subscription base. Other components fill the remainder." },
  { key: "similar_days", term: "similarity", label: "Publish time window", before: "alike within", after: "days",
    hint: "Publish-time similarity is 100% for posts published together, falling evenly to 0% this many days apart. Only posts from the same subscription are compared." },
  { key: "similar_publish_weight", term: "similarity", label: "Publish time similarity", before: "", after: "weight",
    hint: "How much publish time contributes compared with content length and titles. Equal weights give components equal influence; only their ratios matter. Set both bases and all three weights to 0 to turn both similarity penalties off." },
  { key: "similar_title_weight", term: "similarity", label: "Title similarity", before: "", after: "weight",
    hint: "Twice the shared meaningful title words divided by the total words in both titles. Ignores case, punctuation and common English words; keeps numbers. Adds to publish-time and length similarity, normalized by the available weights. Empty titles are left out. Comparisons stay within the same subscription." },
  { key: "similar_length_weight", term: "similarity", label: "Content length similarity", before: "", after: "weight",
    hint: "Length similarity is shorter minutes divided by longer minutes: equal lengths are 100%, 10 and 20 minutes are 50%. Its contribution adds to publish time and is normalized by the weights. Unknown lengths are left out of the weighted average. Every item is 100% similar to itself." },
];
// Every setting, including the second box some have, such as Similar's hours
const KEYS = SETTINGS.flatMap(({ key, extra }) => (extra ? [key, extra.key] : [key]));
let limits = {};  // each term's {min, max, integer}, from the server
let saved = {};  // as saved on the server
let draft = {};  // as shown in the panel
let defaults = {};
const shownTerms = new Set();  // terms in the panel and the formula; ones that are 0 start hidden

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

// The slider position closest to a typed number, on the slider's log scale.
function nearestStep(list, value) {
  const f = (x) => (x === 0 ? 0 : Math.sign(x) * (Math.log10(Math.abs(x)) + 4));
  let best = 0;
  list.forEach((v, i) => { if (Math.abs(f(v) - f(value)) < Math.abs(f(list[best]) - f(value))) best = i; });
  return best;
}

const fmt = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 3 }).replaceAll(",", "\u202f").replace("-", "−");
const plus = (n) => (n < 0 ? ` − ${fmt(-n)}` : ` + ${fmt(n)}`);  // " + 5" or " − 5", for adding a term to a sum
const sameTerms = (a, b) => KEYS.every((key) => a[key] === b[key]);
const inRange = (key, v) => Number.isFinite(v) && v >= limits[key].min && v <= limits[key].max && (!limits[key].integer || Number.isInteger(v));
const controls = {};

function buildSettings() {
  document.getElementById("settings").replaceChildren(...SETTINGS.map(({ key, term, label, before, after, extra, hint }) => {
    const { min, max } = limits[key];
    const list = max <= 100 ? Array.from({ length: max + 1 }, (_, i) => i) : min < 0 ? SIGNED_STEPS : BONUS_STEPS;
    const num = el("input", { type: "number", id: `w-${key}`, step: "any", inputmode: "decimal", required: "", min, max });
    const slider = el("input", { type: "range", min: 0, max: list.length - 1, step: 1, "aria-label": `${label} slider` });
    const def = el("span");
    controls[key] = { num, slider, list, def };
    slider.addEventListener("input", () => {
      num.value = list[slider.value];
      change(key, list[slider.value]);
    });
    num.addEventListener("input", () => {
      const v = num.value.trim() === "" ? NaN : Number(num.value);
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
      const value = () => (box.value.trim() === "" ? NaN : Number(box.value));
      box.addEventListener("input", () => {
        if (inRange(extra.key, value())) change(extra.key, value());
      });
      box.addEventListener("change", save);
      second = [box, extra.after || ""];
    }
    return el("div", { class: "setting", "data-term": term || key },
      el("label", { for: `w-${key}` }, label),
      el("span", { class: "num" }, before, num, after, ...second),
      slider,
      el("p", { class: "hint" }, `${hint} `, def));
  }));
}

function showSettings(w) {
  for (const { key, extra } of SETTINGS) {
    const { num, slider, list } = controls[key];
    if (document.activeElement !== num) num.value = w[key];
    slider.value = nearestStep(list, w[key]);
    if (extra && document.activeElement !== controls[extra.key].num) controls[extra.key].num.value = w[extra.key];
  }
  showFormula();
}

// Hide the terms that are 0 when the page loads; "Add a term" brings them back.
function hideZeroTerms(w) {
  for (const { key, term } of SETTINGS) if (!term && (key === "enrich_count" || w[key] !== 0)) shownTerms.add(key);
  showTerms();
}

function showTerms() {
  for (const row of document.querySelectorAll(".setting")) row.hidden = row.dataset.term === "similarity"
    ? !(shownTerms.has("similar_penalty") || shownTerms.has("above_similarity_penalty"))
    : !shownTerms.has(row.dataset.term);
  const hidden = SETTINGS.filter(({ key, term }) => !term && !shownTerms.has(key));
  const select = document.getElementById("add-term");
  select.replaceChildren(el("option", { value: "" }, "Add a term…"),
    ...hidden.map(({ key, label }) => el("option", { value: key }, label)));
  select.hidden = !hidden.length;
  showFormula();
}

document.getElementById("add-term").addEventListener("change", (e) => {
  const key = e.target.value;
  if (!key) return;
  shownTerms.add(key);
  showTerms();
  controls[key].num.focus();
});

function showFormula() {
  const w = draft;
  const terms = [
    ["new_sub_bonus", `${plus(w.new_sub_bonus)} if nothing opened from its subscription`],
    ["series_penalty", `${plus(w.series_penalty)} if nothing opened before it in its series`],
    ["similar_penalty", `${plus(w.similar_penalty)} × similarity (${fmt(w.similar_base_percent)}% base, ${fmt(Math.max(w.similar_base_percent, w.similar_series_percent))}% in a series; publish time ${fmt(w.similar_publish_weight)} : length ${fmt(w.similar_length_weight)} : title ${fmt(w.similar_title_weight)}) × time remaining (fades to zero over ${fmt(w.similar_hours)} hours; strongest match)`],
    ["video_bonus", `${plus(w.video_bonus)} if video`],
    ["youtube_shorts_bonus", `${plus(w.youtube_shorts_bonus)} if YouTube Shorts`],
    ["image_bonus", `${plus(w.image_bonus)} if image`],
    ["article_bonus", `${plus(w.article_bonus)} if article`],
    ["length_per_minute", `${plus(w.length_per_minute)} × |estimated minutes − ${fmt(w.length_target_minutes)} target minutes|`],
    ["age_per_year", `${plus(w.age_per_year)} × age in years`],
    ["green_points", `${plus(w.green_points)} if tagged “show again soon”`],
    ["white_points", `${plus(w.white_points)} if tagged “maybe later”`],
    ["red_points", `${plus(w.red_points)} if tagged “don't show again”`],
    ["above_similarity_penalty", `${plus(w.above_similarity_penalty)} × summed similarity to posts above`],
    ["task_deadline_bonus", `${plus(w.task_deadline_bonus)} × task urgency (ramps over the final 30 days)`],
    ["task_bonus", `${plus(w.task_bonus)} if task`],
    ["task_priority_bonus", `${plus(w.task_priority_bonus)} × (10 − task priority) / 9`],
  ].filter(([key]) => shownTerms.has(key)).map(([, text]) => text).join("");
  const next = shownTerms.has("next_up_bonus") ? `next up: ${fmt(w.next_up_bonus)} · ` : "";
  const opened = shownTerms.has("seen_bonus") ? `opened: ${fmt(w.seen_bonus)} · ` : "";
  if (typeof relabelDots === "function") relabelDots();  // the dots on the cards show these points
  document.getElementById("formula").textContent = `${next}${opened}all:${terms || " 0"}`;
}

function panelSay(text, isError = false) {
  const status = document.getElementById("panel-status");
  status.textContent = text;
  status.className = isError ? "error" : "";
}

// A setting moved: show the feed in its new order right away, from the top.
function change(key, value) {
  draft = { ...draft, [key]: value };
  showFormula();
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
  for (const { key, extra } of SETTINGS) {
    const second = extra ? ` and ${fmt(defaults[extra.key])} ${extra.after}` : "";
    controls[key].def.textContent = `Default: ${fmt(defaults[key])}${second}.`;
  }
  showSettings(draft);
  hideZeroTerms(saved);
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

