const subscriptionPage = location.pathname === "/subscription" || location.pathname === "/subscription/next-up";
const postPage = location.pathname === "/post";
const postUrl = new URLSearchParams(location.search).get("url") || "";
let postData = null;
let subData = null;
let currentSubView = (new URLSearchParams(location.search).get("view") || (location.pathname === "/subscription/next-up" ? "next-up" : "all")).toLowerCase();
const searchEl = document.getElementById("search");
searchEl.value = new URLSearchParams(location.search).get("q") || "";
let searchTimer;
function searchChanged() {
  clearTimeout(searchTimer);
  limit = PAGE;
  stay.clear();
  shown = [];
  feedTag = null;
  const url = new URL(location.href);
  if (searchEl.value.trim()) url.searchParams.set("q", searchEl.value.trim());
  else url.searchParams.delete("q");
  history.replaceState(null, "", url);
  load();
}
searchEl.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchChanged, 250); });
document.getElementById("search-form").addEventListener("submit", (e) => { e.preventDefault(); searchChanged(); });

const sections = new Map();
const feedEl = document.getElementById("feed");
const statusEl = document.getElementById("status");
let items = [];
const PAGE = 300;  // items per "Show more"
let limit = PAGE;
let pollTimer;
const cards = new Map();  // section + URL -> cached card; one post can appear in several feeds
// Cards you opened or colored on this page stay where they are until you reload it, even though
// their score has changed, so nothing moves away under you.
const stay = new Set();
let shown = [];  // items as last shown
let playingCard = null;
// The playing card stays in place while its surrounding feed updates.

// Fields that change when you open or color an item, or when its subscription gets a new post. They're
// updated in place, not by rebuilding the card, so a video you just started keeps playing.
const VIEW_FIELDS = ["color", "last_viewed", "skipped", "next_after", "series_first", "similar", "sub_last_viewed",
  "score", "reasons", "score_terms", "recent_match", "recent_matches", "above_matches"];
const contentKey = (item) => JSON.stringify(item, (k, v) => (VIEW_FIELDS.includes(k) ? undefined : v));

async function post(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true });
}

// A card keeps the item it was built with, which can be older than the one shown now. Changes go to
// the one shown, which the card keeps showing while it stays in place below what the server sends.
const latest = (item) => shown.find((it) => it.url === item.url) || item;

// Opening an item (its link, or playing its video) records it as viewed.
async function opened(item) {
  item = latest(item);
  stay.add(item.url);
  item.last_viewed = Date.now() / 1000;
  item.skipped = false;
  for (const c of cards.values()) if (c.url === item.url) showViewState(c.el, item);
  await post("/api/viewed", { url: item.url }).catch(() => {});
  load();
}

// Colors don't have proper names yet. Green items rank higher, white is the default, and red
// items sink to the bottom. The card's score details show the exact points.
const COLORS = ["green", "white", "red"];
const COLOR_TITLES = {
  green: "Green: ranked higher",
  white: "White (default): skip, like viewing it but without making what comes after it next up",
  red: "Red: ranked at the bottom",
};

// White also skips the item: it scores as if you'd viewed it, but neither it nor what comes after it is next up.
async function setColor(item, color) {
  item = latest(item);
  const skip = color === "white";
  if (item.color === color && !skip) return;
  item.color = color;
  stay.add(item.url);
  if (skip) Object.assign(item, { last_viewed: Date.now() / 1000, skipped: true });
  for (const c of cards.values()) if (c.url === item.url) showViewState(c.el, item);  // show the new color right away
  await post("/api/color", { url: item.url, color }).catch(() => {});  // white also skips it
  load();
}

function colorDots(item) {
  return el("span", { class: "dots", role: "group", "aria-label": "Color" }, ...COLORS.map((color) => {
    const dot = el("button", { type: "button", class: `dot ${color}`, title: COLOR_TITLES[color], "aria-label": color, "aria-pressed": String(item.color === color) });
    dot.addEventListener("click", () => setColor(item, color));
    return dot;
  }));
}

function activatePlayer(item, button) {
  const article = button.closest("article");
  if (playingCard && playingCard !== article) {
    for (const c of cards.values()) if (c.el === playingCard) {
      const replacement = card(c.item);
      showViewState(replacement, latest(c.item));
      c.el.replaceWith(replacement);
      c.el = replacement;
    }
  }
  playingCard = article;
}

// YouTube: show the thumbnail and only load the player when clicked, so a feed full of videos stays fast.
function youtube(item) {
  const btn = el("button", { class: "play", type: "button", "aria-label": `Play ${item.title}` },
    el("img", { class: "media", loading: "lazy", alt: "", src: `https://i.ytimg.com/vi/${encodeURIComponent(item.youtube)}/hqdefault.jpg` }));
  btn.addEventListener("click", () => {
    activatePlayer(item, btn);
    btn.replaceWith(el("iframe", {
      class: "media", allowfullscreen: "", allow: "autoplay; encrypted-media; picture-in-picture",
      src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(item.youtube)}?autoplay=1`,
    }));
    opened(item);
  });
  return btn;
}

// Jellyfin: the same, with a video the feed server relays from Jellyfin. Files the browser can't play as they
// are come converted to HLS. Items without a picture get none, so the card just links to Jellyfin, and
// a video that can't play here says so and links there instead.
function jellyfinVideo(item) {
  const img = el("img", { class: "media", loading: "lazy", alt: "", src: item.image });
  const btn = el("button", { class: "play", type: "button", "aria-label": `Play ${item.title}` }, img);
  img.onerror = () => btn.remove();
  const unplayable = (reason) => el("div", { class: "media unplayable" }, `Couldn't play this here: ${reason}.`,
    el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer" }, "Open in Jellyfin"));
  btn.addEventListener("click", async () => {
    activatePlayer(item, btn);
    opened(item);
    const { data } = await api(`/api/jellyfin/play?url=${encodeURIComponent(item.url)}`);
    if (!btn.isConnected || playingCard !== btn.closest("article")) return;
    const video = el("video", { class: "media", controls: "", autoplay: "", playsinline: "" });
    if (data.src && (!data.hls || video.canPlayType("application/vnd.apple.mpegurl"))) {
      // Such as Jellyfin failing to convert the video, or the browser failing to decode it
      video.addEventListener("error", () => video.replaceWith(unplayable(
        video.error?.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED ? "Jellyfin couldn't send it in a format this browser plays"
          : "playback failed")));
      video.src = data.src;
      btn.replaceWith(video);
    } else {
      btn.replaceWith(unplayable(data.error || "this browser can't play Jellyfin's converted video"));
    }
  });
  return btn;
}

// A page with a video file, such as an episode on a site without YouTube: the same, playing the file itself.
function pageVideo(item) {
  const img = el("img", { class: "media", loading: "lazy", alt: "", src: item.image });
  const btn = el("button", { class: "play", type: "button", "aria-label": `Play ${item.title}` }, img);
  img.onerror = () => btn.remove();
  btn.addEventListener("click", () => {
    activatePlayer(item, btn);
    const video = el("video", { class: "media", controls: "", autoplay: "", playsinline: "", src: item.video });
    video.addEventListener("error", () => video.replaceWith(el("div", { class: "media unplayable" }, "Couldn't play this here.",
      el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer" }, "Open its page"))));
    btn.replaceWith(video);
    opened(item);
  });
  return btn;
}

function card(item) {
  const title = item.loading ? "Loading…" : item.title || item.url;
  const domain = item.domain || new URL(item.url).hostname;
  let media = null;
  if (item.youtube) media = youtube(item);
  else if (item.jellyfin && item.image) media = jellyfinVideo(item);
  else if (item.video && item.image) media = pageVideo(item);
  else if (item.image) {
    media = el("img", { class: "media", loading: "lazy", alt: "", src: item.image });
    media.onerror = () => media.remove();
  }
  const icon = item.icon ? el("img", { src: item.icon, alt: "" }) : null;
  if (icon) icon.onerror = () => icon.remove();
  const source = item.kind === "subscription" ? item.feed : item.site || domain;
  // Older YouTube videos only have dates like "3 years ago", so theirs are approximate. Jellyfin's are when
  // the movie or episode was added to the server.
  const relative = (unix) => {
    const seconds = Math.abs(Date.now() / 1000 - unix);
    const unit = seconds >= 86400 ? 86400 : seconds >= 3600 ? 3600 : 60;
    const amount = Math.max(1, Math.round(seconds / unit));
    const name = unit === 86400 ? "day" : unit === 3600 ? "hour" : "minute";
    return unix > Date.now() / 1000 ? `in ${amount} ${name}${amount === 1 ? "" : "s"}` : `${amount} ${name}${amount === 1 ? "" : "s"} ago`;
  };
  const calendarDate = item.calendar_kind === "event" ? item.calendar_start : item.deadline;
  const when = item.calendar_kind
    ? calendarDate ? `${item.calendar_kind === "event" ? "starts" : "due"} ${relative(calendarDate)}` : "no deadline"
    : item.kind === "link" || item.jellyfin ? `added ${ago(item.date)}`
      : item.date_approx ? `about ${ago(item.date)}` : ago(item.date);

  const body = el("div", { class: "body" },
    el("div", { class: "source" }, icon, `${source} · ${when}`),
    el("h2", {}, title),
    item.description ? el("p", {}, item.description) : null,
    item.tags.length ? el("div", { class: "tags" }, ...item.tags.map((t) => el("span", {}, t))) : null,
  );
  const link = el("a", { class: "link", href: item.url, target: "_blank", rel: "noopener noreferrer" }, body);
  link.addEventListener("click", () => opened(item));
  link.addEventListener("auxclick", (e) => { if (e.button === 1) opened(item); });  // middle-click
  const art = el("article", item.loading ? { class: "loading" } : {});
  if (media && !item.youtube && !item.jellyfin && !item.video) link.prepend(media);
  else if (media) art.append(media);
  art.append(link);
  if (item.youtube && item.youtube_playlists?.length) {
    art.append(el("div", { class: "playlists" }, ...item.youtube_playlists.map((id, i) =>
      el("a", { href: `https://www.youtube.com/playlist?list=${encodeURIComponent(id)}`,
        target: "_blank", rel: "noopener noreferrer" },
        item.youtube_playlists.length === 1 ? "YouTube playlist ↗" : `YouTube playlist ${i + 1} ↗`))));
  }
  art.append(el("div", { class: "playlists" }, el("a", {
    class: "post-link", href: `/post?url=${encodeURIComponent(item.url)}`,
  }, "Post & related")));
  if (item.sub) art.append(el("div", { class: "playlists" }, el("a", {
    href: `/subscription?url=${encodeURIComponent(item.sub)}`,
  }, `${source} · Subscription`)));
  if (item.related_percent != null) art.append(el("div", { class: "playlists" }, `${item.related_percent}% similar`));
  art.append(why(item), el("div", { class: "viewed" }));
  return art;
}

// The score, color and last-viewed line change without rebuilding the card, so a playing video keeps playing.
function showViewState(art, item) {
  const text = item.last_viewed ? `${item.skipped ? "Skipped" : "Viewed"} ${ago(item.last_viewed)}` : "";
  art.querySelector(".viewed").replaceChildren(el("span", {}, text), colorDots(item));
  const old = art.querySelector(".why"), details = why(item);
  if (old.outerHTML === details.outerHTML) return;
  details.open = old.open;
  if (details.open) details.dispatchEvent(new Event("toggle"));
  if (old.querySelector(".additional-matches[open]")) {
    const more = details.querySelector(".additional-matches");
    if (more) more.open = true;
  }
  old.replaceWith(details);
}

// The server's order, except that cards in stay keep the place they had, even when their new
// score puts them below the items the server sent, like a card you just made red.
function arrange(list) {
  const byUrl = new Map([...shown.map((it) => [it.url, it]), ...list.map((it) => [it.url, it])]);
  const kept = shown.map((it, i) => [it.url, i]).filter(([url]) => stay.has(url));
  const keptUrls = new Set(kept.map(([url]) => url));
  const out = list.filter((it) => !keptUrls.has(it.url));
  for (const [url, i] of kept) out.splice(i, 0, byUrl.get(url));
  return out;
}

function render() {
  if (postPage) return renderPost();
  if (subscriptionPage) return renderSubscription();
  const list = arrange(items);
  shown = list;
  renderList(feedEl, list, "feed");
  renderStopNotice();
}

function renderSubscription() {
  if (!subData) return;
  const nextUp = subData.next_up || [];
  const isNextUpOnly = currentSubView === "next-up";
  const search = searchEl.value.trim();
  const groups = isNextUpOnly
    ? [{
        id: "next-up",
        title: "Next up",
        note: "Every next up post in this subscription",
        items: nextUp,
        empty: search ? "No next up posts match your search." : "No next up posts in this subscription yet."
      }]
    : [
        ...(nextUp.length ? [{ id: "next-up", title: "Next up", note: "Next in each series or after what you opened", items: nextUp }] : []),
        { id: "posts", title: nextUp.length ? "All posts" : "Posts", items: arrange(items), empty: search ? "No posts match your search." : "No posts in this subscription yet." },
      ];
  shown = [...new Map(groups.flatMap((g) => g.items).map((it) => [it.url, it])).values()];
  const ids = new Set(groups.map((g) => g.id));
  for (const [id, section] of sections) if (!ids.has(id)) { section.remove(); sections.delete(id); }
  for (const group of groups) {
    let section = sections.get(group.id);
    if (!section) {
      section = el("section", { class: "post-section", "aria-label": group.title },
        el("h2", {}, group.title), el("p", { class: "section-note" }), el("div", { class: "post-section" }));
      sections.set(group.id, section);
    }
    feedEl.append(section);
    const heading = section.querySelector("h2");
    heading.textContent = group.title;
    section.children[1].textContent = group.note || "";
    section.children[1].hidden = !group.note;
    if (!group.items.length && group.empty) {
      section.children[2].replaceChildren(el("p", { class: "empty" }, group.empty));
    } else {
      renderList(section.children[2], group.items, group.id);
    }
  }
  renderStopNotice();
}

function renderList(container, list, prefix) {
  const nodes = list.map((item) => {
    const key = contentKey(item), id = `${prefix}:${item.url}`;
    let c = cards.get(id);
    if (!c || c.key !== key) {
      c = { key, el: card(item), url: item.url, item, prefix };
      cards.set(id, c);
    }
    c.item = item;
    showViewState(c.el, item);
    return c.el;
  });
  const urls = new Set(list.map((it) => it.url));
  for (const [id, c] of cards) if (c.prefix === prefix && !urls.has(c.url)) cards.delete(id);

  if (!nodes.length) {
    container.replaceChildren(el("div", { class: "empty" }, "Nothing here yet. Add a link or subscribe to a feed above."));
    return;
  }
  // Moving a card takes it out of the page and puts it back, which restarts a video playing in it.
  // So the playing card never moves: the cards before and after it are arranged around it, and only
  // cards that are out of place move. Cards that are going away (or being rebuilt) are removed first.
  const keep = new Set(nodes);
  for (const child of [...container.children]) if (!keep.has(child)) child.remove();
  const playingEl = playingCard;
  let at = nodes.indexOf(playingEl);
  if (at < 0 || !playingEl.isConnected) at = 0;
  if (!nodes[at].isConnected) container.prepend(nodes[at]);
  for (let i = at - 1, next = nodes[at]; i >= 0; next = nodes[i--]) {
    if (nodes[i].nextSibling !== next) container.insertBefore(nodes[i], next);
  }
  for (let i = at + 1, prev = nodes[at]; i < nodes.length; prev = nodes[i++]) {
    if (prev.nextSibling !== nodes[i]) container.insertBefore(nodes[i], prev.nextSibling);
  }
}

function renderPost() {
  if (!postData) return;
  document.getElementById("post-loading")?.remove();
  const groups = [
    { id: "post", title: "Post", items: [postData.item] },
    ...postData.series.map((g) => ({ ...g, note: "In series order" })),
    ...(!postData.series.length ? [{ id: "no-series", title: "Series", items: [], note: "This post is not part of a known series." }] : []),
    { id: "similar", title: "Similar posts", note: "Most similar first · same subscription", ...postData.similar },
  ];
  shown = [...new Map(groups.flatMap((g) => g.items).map((it) => [it.url, it])).values()];
  const ids = new Set(groups.map((g) => g.id));
  for (const [id, section] of sections) if (!ids.has(id)) { section.remove(); sections.delete(id); }
  for (const group of groups) {
    let section = sections.get(group.id);
    if (!section) {
      section = el("section", { class: "post-section", "aria-label": group.title },
        el("h2", {}, group.title), el("p", { class: "section-note" }), el("div", { class: "post-section" }));
      sections.set(group.id, section);
      feedEl.insertBefore(section, group.id === "similar" ? null : sections.get("similar") || null);
    }
    const heading = section.querySelector("h2");
    if (group.url) heading.replaceChildren(el("a", { href: group.url, target: "_blank", rel: "noopener noreferrer" }, `${group.title} ↗`));
    else heading.textContent = group.title;
    section.children[1].textContent = group.note || "";
    section.children[1].hidden = !group.note;
    const container = section.children[2];
    renderList(container, group.items, group.id);
    if (!group.items.length) container.replaceChildren(...(group.id === "similar" ? [el("p", { class: "section-note" }, "No similar posts found with the current similarity settings.")] : []));
  }
  for (const [id, c] of cards) if (!ids.has(c.prefix)) cards.delete(id);
  renderStopNotice();
}

const fmtPoints = (n) => (n > 0 ? "+" : n < 0 ? "−" : "±") + Math.round(Math.abs(n) * 100) / 100;

// Shows why the card is where it is: the ranker's reasons and the points each one added.
function scoreDetail(item, reason) {
  const w = item.score_terms;
  if (!w) return "";
  const label = reason.label, n = (v) => String(Math.round(v * 100) / 100).replace("-", "−");
  const rate = (v) => String(v).replace("-", "−");
  if (label.startsWith("similarity to ")) {
    const match = label.match(/similarity to (\d+) posts? above, (\d+)% total/);
    return match ? `${rate(w.above_similarity_penalty)} × ${match[2]}% similarity · ${match[1]} above` : "";
  }
  if (item.similar && label.includes("% like")) {
    const like = item.similar;
    const remaining = 100 * (1 - like.hours / w.similar_hours);
    const strongest = `${like.percent}% similarity × ${n(remaining)}% remaining · ${like.hours}/${rate(w.similar_hours)} h elapsed`;
    return like.matches > 1
      ? `${rate(w.similar_penalty)} × ${n(like.total)}% combined · ${like.matches} matches · strongest ${strongest}`
      : `${rate(w.similar_penalty)} × ${strongest}`;
  }
  if (label.startsWith("estimated ")) return `|${n(item.estimated_minutes)} − ${rate(w.length_target_minutes)}| min × ${rate(w.length_per_minute)} pts/min`;
  if (label.startsWith("age ")) {
    return `${rate(w.age_per_year)} pts/year`;
  }
  return "";
}

function highlightedTitle(match) {
  return (match.title_parts || [{ text: match.title, shared: false }])
    .map((part) => part.shared ? el("mark", {}, part.text) : part.text);
}

function matchBreakdown(match) {
  const n = (v) => Math.round(v * 100) / 100;
  const available = match.components.filter((c) => c.percent != null);
  const totalWeight = available.reduce((sum, c) => sum + c.weight, 0);
  const base = match.base_percent || 0;
  const components = base ? [
    { label: "Same subscription", percent: 100, weight: base },
    ...match.components.map((c) => ({ ...c,
      weight: c.percent == null || !totalWeight ? 0 : (100 - base) * c.weight / totalWeight })),
  ] : match.components;
  const denominator = base ? 100 : totalWeight;
  const terms = components.filter((c) => c.percent != null && c.percent * c.weight !== 0);
  const calculation = `(${terms.map((c) => `${n(c.percent)}% × ${n(c.weight)}`).join(" + ") || "0"}) ÷ ${denominator} ≈ ${match.percent}%`;
  return el("div", { class: "similar-match" },
    el("div", { class: "match-title" }, `${match.percent}% · `,
      el("a", { href: match.url, target: "_blank", rel: "noopener noreferrer" }, ...highlightedTitle(match))),
    ...(match.self ? [el("div", {}, "Same item · 100% match")] : [
      ...components.filter((c) => c.weight !== 0 && c.percent !== 0).map((c) => el("div", {},
        el("div", { class: "match-component" },
          el("span", {}, c.label), c.input ? el("span", {}, c.input) : null,
          el("span", {}, c.percent == null ? "—" : `${n(c.percent)}% × ${n(c.weight)}`)))),
      el("div", { class: "match-average" },
        calculation),
    ]));
}

function similarityMatches(item, reason) {
  if (reason.label.startsWith("similarity to ") && item.above_matches?.length) {
    const matches = item.above_matches;
    return el("div", { class: "match-list" },
      el("div", { class: "reason-detail" }, `${matches.length} from same subscription · ${matches.reduce((sum, m) => sum + m.percent, 0)}% combined`),
      ...matches.slice(0, 3).map(matchBreakdown),
      matches.length > 3 ? lazyDetails({ class: "additional-matches" },
        `${matches.length - 3} more match${matches.length === 4 ? "" : "es"}`,
        () => matches.slice(3).map(matchBreakdown)) : null);
  }
  if (reason.label.includes("% like") && item.recent_matches?.length) {
    const matches = item.recent_matches;
    const n = (v) => Math.round(v * 100) / 100;
    return el("div", { class: "match-list" },
      el("div", { class: "reason-detail" }, `${matches.length} recently ${item.similar.skipped ? "skipped" : "opened"} · ${n(item.similar.total)}% combined strength`),
      ...matches.slice(0, 3).map(matchBreakdown),
      matches.length > 3 ? lazyDetails({ class: "additional-matches" },
        `${matches.length - 3} more match${matches.length === 4 ? "" : "es"}`,
        () => matches.slice(3).map(matchBreakdown)) : null);
  }
  return reason.label.includes("% like") && item.recent_match ? matchBreakdown(item.recent_match) : null;
}

// Closed cards and folded match lists do not need their detailed DOM yet.
function lazyDetails(attrs, summary, children) {
  const details = el("details", attrs, el("summary", {}, summary));
  let populated = false;
  details.addEventListener("toggle", () => {
    if (!details.open || populated) return;
    populated = true;
    const content = children();
    details.append(...(Array.isArray(content) ? content : [content]));
  });
  return details;
}

function scoreRows(item) {
  const rows = item.reasons.filter((r) => r.points !== 0).map((r) => el("tr", {},
    el("td", { class: "points" }, fmtPoints(r.points)),
    el("td", {}, el("div", { class: "reason-label" },
        r.label.startsWith("similarity to ") ? "Similar posts above"
          : item.recent_match && r.label.includes("% like") ? `Similar to recently ${item.similar.skipped ? "skipped" : "opened"}`
          : r.label.replace(/, [\d.]+% penalty remaining$/, "")),
      scoreDetail(item, r) ? el("div", { class: "reason-detail" }, scoreDetail(item, r)) : null,
      similarityMatches(item, r))));
  return rows.length ? el("table", { "aria-label": "Bonuses and penalties" }, el("tbody", {}, ...rows))
      : el("div", { class: "reason-detail" }, "No bonuses or penalties apply.");
}

function why(item) {
  return lazyDetails({ class: "why" }, `Score ${Math.round(item.score * 100) / 100}`, () => scoreRows(item));
}

// One request at a time: calls while one is running (such as from a slider being dragged) are
// combined into a single request after it, with the newest settings.
let loading = null;
let loadAgain = false;

function load() {
  if (loading) {
    loadAgain = true;
    return loading;
  }
  loading = fetchFeed().finally(() => {
    loading = null;
    if (loadAgain) {
      loadAgain = false;
      load();
    }
  });
  return loading;
}

let enrichOnLoad = true;
let feedTag = null;  // the ETag of the feed shown: the server answers "not modified" if it's still the same
let lastPending = 0;

async function fetchFeed() {
  clearTimeout(pollTimer);
  try {
    const params = new URLSearchParams({ limit, q: searchEl.value.trim() });
    if (postPage || subscriptionPage) params.set("url", postUrl);
    // Terms you haven't saved yet, such as while you drag a slider, are sent along to preview them.
    if (Object.keys(saved).length && !sameTerms(draft, saved)) params.set("terms", JSON.stringify(draft));
    if (enrichOnLoad) params.set("enrich", "1");
    const res = await fetch(`/api/${postPage ? "post" : subscriptionPage ? "subscription" : "feed"}?${params}`,
      { cache: "no-store", headers: feedTag ? { "If-None-Match": feedTag } : {} });
    if (res.ok || res.status === 304) enrichOnLoad = false;
    if (res.status === 304) {
      render();  // same items, but cards kept in place may now take their place
      pollTimer = setTimeout(load, pollDelay(lastPending, lastStop?.stop_at));
      return;
    }
    const data = await res.json();
    if (!res.ok) {
      if ((postPage || subscriptionPage) && res.status === 404) {
        postData = null;
        sections.clear();
        cards.clear();
        shown = [];
        document.getElementById("more").hidden = true;
      }
      throw new Error(data.error);
    }
    feedTag = res.headers.get("ETag");
    lastPending = data.pending;
    lastStop = data;
    forceStop(data);
    if (postPage) showPost(data);
    else if (subscriptionPage) showSubscription(data);
    else showFeed(data);
    pollTimer = setTimeout(load, pollDelay(data.pending, data.stop_at));
  } catch (error) {
    statusEl.textContent = error.message || "Server unreachable";
    if (postPage && !postData) feedEl.replaceChildren(el("p", { role: "status", id: "post-loading" }, error.message || "Couldn't load this post."));
    if (subscriptionPage && !subData) feedEl.replaceChildren(el("p", { role: "status", id: "post-loading" }, error.message || "Couldn't load this subscription."));
    pollTimer = setTimeout(load, 5000);
  }
}

// The status line: calendars that didn't answer, the stop time, if any, then text, then how many
// fetches are under way.
function showStatus(data, text) {
  const unreachable = (data.calendars_unreachable || []).map((c) =>
    `Calendar “${c.title}” isn't answering; using its events from ${c.fetched ? clock(c.fetched) : "before"}`);
  statusEl.textContent = [...unreachable, stopNote(data.stop_at, data.stopped), text, data.pending ? `fetching ${data.pending}…` : ""]
    .filter(Boolean).join(" · ");
}

// How many items are shown and how they're ranked, with "Show more" when there are more.
function showRanked(data) {
  const more = document.getElementById("more");
  more.hidden = data.total <= items.length;
  more.textContent = `Show ${Math.min(PAGE, data.total - items.length)} more (${data.total - items.length} not shown)`;
  showStatus(data, `${data.total > items.length ? `top ${items.length} of ${data.total}` : data.total} items · ranked by ${data.ranker}`);
  rankerNote(data.ranker);
}

function showFeed(data) {
  items = data.items;
  render();
  if (!items.length) feedEl.replaceChildren(el("p", {}, searchEl.value.trim() ? "No posts match your search." : data.stopped ? "Stopped: the stop time has passed." : data.stop_at ? "Everything left would run past the stop time." : "No posts in your feed yet."));
  showRanked(data);
}

function showPost(data) {
  postData = data;
  document.title = `${data.item.title || "Post"} · Content Feed`;
  render();
  showStatus(data, "");
  const remaining = Math.max(0, data.similar.total - data.similar.items.length,
    ...data.series.map((g) => g.total - g.items.length));
  const more = document.getElementById("more");
  more.hidden = !remaining;
  more.textContent = "Show more related posts";
}

function showSubscription(data) {
  subData = data;
  const sub = data.subscription;
  if (sub) {
    document.getElementById("subscription-title").textContent = sub.title;
    document.getElementById("subscription-info").replaceChildren(
      el("a", { href: sub.site || sub.url, target: "_blank", rel: "noopener noreferrer" }, "Visit source ↗"),
      sub.tags.length ? ` · ${sub.tags.join(" · ")}` : "",
      sub.error ? ` · Last check failed: ${sub.error}` : "");
    const stats = [
      ["Posts", sub.posts, "all"],
      ["Next up", sub.next_up, "next-up"],
      ["Opened", sub.opened], ["Skipped", sub.skipped], ["Unseen", sub.unseen],
      ["Green", sub.color_counts.green], ["White", sub.color_counts.white], ["Red", sub.color_counts.red],
      ["Latest post", sub.latest ? ago(sub.latest) : "None yet"],
      ["Last viewed or skipped", sub.last_viewed ? ago(sub.last_viewed) : "Never"],
      ["Last checked", sub.checking ? "Checking…" : sub.fetched ? ago(sub.fetched) : "Not yet"],
    ];
    document.getElementById("subscription-stats").replaceChildren(...stats.map(([label, value, view]) => {
      const itemEl = el("div", view ? { class: "clickable-stat", title: `View ${label.toLowerCase()}` } : {},
        el("dt", {}, label), el("dd", {}, String(value)));
      if (view) itemEl.addEventListener("click", () => setSubView(view));
      return itemEl;
    }));
  }
  items = data.items;
  showSubView();
}

document.getElementById("more").addEventListener("click", (e) => {
  limit += PAGE;
  e.target.disabled = true;
  load().finally(() => { e.target.disabled = false; });
});

document.getElementById("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("url");
  const subscribing = e.submitter?.value === "subscribe";
  say(subscribing ? "Looking for a feed…" : "");
  const { ok, data } = await api(subscribing ? "/api/subscriptions" : "/api/links", { url: input.value });
  if (ok) {
    input.value = "";
    say(subscribing ? "Subscribed." : "");
  } else {
    say(subscribing ? `Couldn't subscribe: ${data.error || "unknown error"}` : "Couldn't add that link (invalid or already in the feed).", true);
  }
  if (subscribing) loadSubscriptionCount();
  load();
});

document.getElementById("refresh").addEventListener("click", async () => {
  await api("/api/refresh", {});
  load();
});

async function loadSubscriptionCount() {
  const { ok, data } = await api("/api/subscriptions");
  if (ok) document.getElementById("subs-link").textContent = `Subscriptions (${data.subscriptions.length})`;
}

function setSubView(view) {
  currentSubView = view;
  const url = new URL(location.href);
  if (view === "next-up") url.searchParams.set("view", "next-up");
  else url.searchParams.delete("view");
  history.replaceState(null, "", url);
  showSubView();
}

// The subscription page's title, view buttons, status line and "Show more" for the view chosen.
function showSubView() {
  const nextUp = subData?.next_up?.length ?? 0;
  if (subData?.subscription) {
    document.title = currentSubView === "next-up"
      ? `${subData.subscription.title} · Next up · Content Feed`
      : `${subData.subscription.title} · Content Feed`;
  }
  updateSubViewButtons(nextUp);
  if (subData) {
    if (currentSubView === "next-up") {
      document.getElementById("more").hidden = true;
      showStatus(subData, `${nextUp} next up item${nextUp === 1 ? "" : "s"}`);
    } else {
      showRanked(subData);
    }
  }
  render();
}

function updateSubViewButtons(count = 0) {
  const container = document.getElementById("subscription-views");
  if (!container) return;
  const allBtn = container.querySelector('[data-view="all"]');
  const nextUpBtn = container.querySelector('[data-view="next-up"]');
  if (allBtn) allBtn.className = currentSubView !== "next-up" ? "view-btn active" : "view-btn";
  if (nextUpBtn) {
    nextUpBtn.className = currentSubView === "next-up" ? "view-btn active" : "view-btn";
    nextUpBtn.textContent = count ? `Next up (${count})` : "Next up";
  }
}

if (subscriptionPage) {
  document.querySelector("h1").replaceChildren(el("a", { href: "/" }, "← Content Feed"));
  document.getElementById("add").hidden = true;
  searchEl.placeholder = "Search posts and next up";
  searchEl.setAttribute("aria-label", "Search posts and next up");
  const viewNav = el("div", { class: "bar subscription-views", id: "subscription-views" },
    el("button", { type: "button", class: "view-btn active", "data-view": "all" }, "All posts"),
    el("button", { type: "button", class: "view-btn", "data-view": "next-up" }, "Next up"));
  viewNav.querySelector('[data-view="all"]').addEventListener("click", () => setSubView("all"));
  viewNav.querySelector('[data-view="next-up"]').addEventListener("click", () => setSubView("next-up"));
  feedEl.before(el("div", { class: "bar" }, el("h2", { id: "subscription-title" }, "Subscription")),
    el("div", { class: "bar", id: "subscription-info" }),
    el("dl", { class: "subscription-stats", id: "subscription-stats", "aria-label": "Subscription statistics" }),
    viewNav);
  updateSubViewButtons(0);
}

if (postPage) {
  document.querySelector("h1").replaceChildren(el("a", { href: "/", class: "post-link" }, "← Content Feed"));
  document.getElementById("add").hidden = true;
  searchEl.placeholder = "Search series and similar posts";
  searchEl.setAttribute("aria-label", "Search series and similar posts");
  document.querySelector("#panel > .hint").textContent = "Series are in episode or playlist order. Similar posts are ordered by similarity using the settings below. Cards show scores before the main feed's adjustments for posts above them.";
  feedEl.append(el("p", { role: "status", id: "post-loading" }, "Loading post…"));
}
loadSubscriptionCount();
loadSettings().catch(() => panelSay("Couldn't load the settings.", true)).finally(load);
