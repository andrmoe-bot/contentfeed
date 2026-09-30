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

const messageEl = document.getElementById("message");

function say(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? "error" : "";
}
