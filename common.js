// Shared by index.html and subscriptions.html

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function ago(unix) {
  const s = Date.now() / 1000 - unix;
  if (s < 60) return "just now";
  for (const [unit, secs] of [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]]) {
    const n = Math.floor(s / secs);
    if (n >= 1) return `${n} ${unit}${n > 1 ? "s" : ""} ago`;
  }
}

// Such as "09:30", for a unix time.
function clock(unix) {
  const d = new Date(unix * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// A time with its day, when that isn't today: "14:30", "midnight" (the end of today), "tomorrow
// 09:00", "Mon 09:00" within the week, else "Mon, Nov 30 09:00".
function when(unix) {
  const d = new Date(unix * 1000), today = new Date();
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(today).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return clock(unix);
  if (days === 1 && d.getHours() === 0 && d.getMinutes() === 0) return "midnight";
  if (days === 1) return `tomorrow ${clock(unix)}`;
  const opts = days > 1 && days < 7 ? { weekday: "short" } : { weekday: "short", day: "numeric", month: "short" };
  return `${d.toLocaleDateString(undefined, opts)} ${clock(unix)}`;
}

// How long until a unix time: "now", "in 5 min", "in 2 h", "in 2 h 30 min".
function inTime(unix) {
  const minutes = Math.round((unix - Date.now() / 1000) / 60);
  if (minutes <= 0) return "now";
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return `in ${[h ? `${h} h` : "", m || !h ? `${m} min` : ""].filter(Boolean).join(" ")}`;
}

// GET path, or with a body, POST it as JSON. Returns { ok, data }; a server that doesn't answer is an error too.
async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined).catch(() => null);
  if (!res) return { ok: false, data: { error: "the server didn't answer" } };
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

const messageEl = document.getElementById("message");

function say(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? "error" : "";
}
