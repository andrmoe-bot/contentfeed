const listEl = document.getElementById("list");
const filterEl = document.getElementById("filter");
const sortEl = document.getElementById("sort");
let subs = [];
const AGE_UNITS = { days: 86400, weeks: 7 * 86400, months: 30 * 86400, years: 365 * 86400 };
let pollTimer;

function show(list) {
  subs = list;
  render();
  // While a check is running, look again shortly so its result appears.
  clearTimeout(pollTimer);
  if (subs.some((s) => s.checking || s.older === "loading")) pollTimer = setTimeout(load, 2000);
}

async function load() {
  const { data } = await api("/api/subscriptions");
  show(data.subscriptions || []);
}

function sorted(list) {
  const by = {
    file: () => 0,
    name: (a, b) => a.title.localeCompare(b.title),
    latest: (a, b) => (b.latest || 0) - (a.latest || 0),
    problems: (a, b) => !!b.error - !!a.error,
  }[sortEl.value];
  return [...list].sort(by);
}

function render() {
  const q = filterEl.value.trim().toLowerCase();
  const shown = sorted(subs).filter((s) => !q || [s.title, s.url, s.feed_url || "", ...s.tags].some((v) => v.toLowerCase().includes(q)));
  const failing = subs.filter((s) => s.error).length;
  document.title = `Subscriptions (${subs.length})`;
  document.querySelector("h1").textContent = `Subscriptions (${subs.length})` + (failing ? ` · ${failing} failing` : "");
  if (!subs.length) {
    listEl.replaceChildren(el("div", { class: "empty" }, "No subscriptions yet. Paste a feed or channel above."));
  } else if (!shown.length) {
    listEl.replaceChildren(el("div", { class: "empty" }, "No subscriptions match that filter."));
  } else {
    listEl.replaceChildren(...shown.map(row));
  }
}

function row(s) {
  const icon = s.icon ? el("img", { src: s.icon, alt: "" }) : null;
  if (icon) icon.onerror = () => icon.remove();

  const statsEl = el("div", { class: "stats" });
  if (s.checking) {
    statsEl.textContent = "Checking…";
  } else if (s.fetched) {
    const parts = [
      `${s.posts} post${s.posts === 1 ? "" : "s"}`,
      s.next_up ? el("a", { href: `/subscription?url=${encodeURIComponent(s.url)}&view=next-up` }, `${s.next_up} next up`) : null,
      s.latest ? `latest ${ago(s.latest)}` : null,
      `checked ${ago(s.fetched)}`,
      s.older === "loading" ? "loading older videos…" : null,
    ].filter(Boolean);
    parts.forEach((p, i) => {
      if (i > 0) statsEl.append(" · ");
      statsEl.append(p);
    });
  } else {
    statsEl.textContent = "Not checked yet";
  }

  const addr = el("div", { class: "addr" }, el("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.url));
  if (s.page) {
    addr.append(el("br"), "No feed; posts are read from the page");
  } else if (s.feed_url && s.feed_url !== s.url) {
    addr.append(el("br"), "Feed: ", el("a", { href: s.feed_url, target: "_blank", rel: "noopener noreferrer" }, s.feed_url));
  }

  // Tags: saved when you press Save or Enter.
  const tagInput = el("input", { type: "text", value: s.tags.join(" "), placeholder: "no tags", "aria-label": `Tags for ${s.title}` });
  const saveBtn = el("button", { type: "submit", disabled: "" }, "Save");
  tagInput.addEventListener("input", () => { saveBtn.disabled = tagInput.value.trim() === s.tags.join(" "); });
  const tagForm = el("form", { class: "tag-edit" }, el("label", {}, "Tags"), tagInput, saveBtn);
  tagForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { ok, data } = await api("/api/subscriptions/tags", { url: s.url, tags: tagInput.value.split(/[\s,]+/) });
    say(ok ? `Saved tags for ${s.title}.` : "Couldn't save tags; is it still in feeds.txt?", !ok);
    if (data.subscriptions) show(data.subscriptions);
  });

  const checkBtn = el("button", { type: "button" }, s.checking ? "Checking…" : "Check now");
  if (s.checking) checkBtn.disabled = true;
  checkBtn.addEventListener("click", async () => {
    checkBtn.disabled = true;
    checkBtn.textContent = "Checking…";
    const { ok, data } = await api("/api/subscriptions/check", { url: s.url });
    if (!ok) say("That subscription is no longer in feeds.txt.", true);
    if (data.subscriptions) show(data.subscriptions);
  });

  // Tag posts older than an age "don't show again" (red), which adds its points (−1000 by default).
  const ageInput = el("input", { type: "number", min: "0", step: "any", value: "1", required: "", "aria-label": `Age for ${s.title}` });
  const unitSelect = el("select", { "aria-label": "Unit" }, ...Object.keys(AGE_UNITS).map((u) => el("option", { value: u }, u)));
  unitSelect.value = "years";
  const redForm = el("form", { class: "red-edit" }, "Tag posts older than", ageInput, unitSelect,
    el("button", { type: "submit" }, "Don't show again"));
  redForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const older = `older than ${ageInput.value} ${unitSelect.value}`;
    if (!confirm(`Tag the posts in ${s.title} ${older} “don't show again”? Posts you've already tagged keep their tag.`)) return;
    const { ok, data } = await api("/api/subscriptions/red", { url: s.url, age: ageInput.value * AGE_UNITS[unitSelect.value] });
    say(ok ? `Tagged ${data.marked} post${data.marked === 1 ? "" : "s"} in ${s.title} ${older} “don't show again”.`
      : "Couldn't tag posts; is it still in feeds.txt?", !ok);
    if (data.subscriptions) show(data.subscriptions);
  });

  const removeBtn = el("button", { type: "button", class: "danger" }, "Unsubscribe");
  removeBtn.addEventListener("click", async () => {
    if (!confirm(`Unsubscribe from ${s.title}? Its posts will disappear from the feed, along with which ones you opened or tagged.`)) return;
    const { ok, data } = await api("/api/subscriptions/remove", { url: s.url });
    say(ok ? `Unsubscribed from ${s.title}.` : "Couldn't unsubscribe; it may already be gone.", !ok);
    if (data.subscriptions) show(data.subscriptions);
  });

  return el("section", { class: s.error ? "sub failed" : "sub" },
    el("div", { class: "title" }, icon, el("a", { href: `/subscription?url=${encodeURIComponent(s.url)}` }, s.title)),
    addr,
    statsEl,
    s.error ? el("div", { class: "error" }, `Last check failed: ${s.error}`) : null,
    s.older_error ? el("div", { class: "error" }, `${s.older_error[0].toUpperCase()}${s.older_error.slice(1)}; trying again later.`) : null,
    tagForm,
    redForm,
    el("div", { class: "actions" }, checkBtn, removeBtn),
  );
}

document.getElementById("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("url");
  const button = e.submitter;
  const subscribing = button?.value !== "link";
  if (button) button.disabled = true;
  say(subscribing ? "Checking subscription…" : "Adding link…");
  const { ok, data } = await api(subscribing ? "/api/subscriptions" : "/api/links", { url: input.value });
  if (button) button.disabled = false;
  if (ok) {
    input.value = "";
    say(subscribing ? "Subscribed." : "Added to links.txt.");
  } else {
    say(subscribing ? `Couldn't subscribe: ${data.error || "unknown error"}`
      : "Couldn't add that link (invalid or already in the feed).", true);
  }
  if (subscribing) load();
});

document.getElementById("refresh").addEventListener("click", async (e) => {
  e.target.disabled = true;
  const { ok, data } = await api("/api/refresh", {});
  e.target.disabled = false;
  say(ok ? "Checking every link and subscription in the background; new posts show up as they arrive."
    : `Couldn't refresh: ${data.error || "unknown error"}`, !ok);
  load();
});

filterEl.addEventListener("input", render);
sortEl.addEventListener("change", render);
load();
