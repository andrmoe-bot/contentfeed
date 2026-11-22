// Stopping for calendar events. While an event's stop time is ahead (its start minus the Event lead
// time setting), posts that would run past it get the "Past the stop time" points for each minute
// they would (as of when the feed was shown: the page doesn't reorder as time passes). When it runs
// out, the page shows the stop screen in JS: the feed is hidden behind a notice — in an event's lead
// time, "stop watching now and get ready", with the event in full and a countdown to its start — and
// the event taking place now, if any, with a countdown to its end; playback is force-stopped and an
// audio alert sounds (see forceStop). Since the feed itself is still served, switching back to the
// feed is seamless once the event is over.

// How long until the next check of the feed: soon while fetches are under way, every 10 seconds while
// a stop time is in effect, else every 30, but always just after the next change the response
// announces: the stop time, the feed returning, or the calendar changing the mode (including turning one
// on when none is).
function pollDelay(data) {
  if (!data) return 30000;
  const base = data.pending ? 1500 : data.stop_at ? 10000 : 30000;
  const now = Date.now() / 1000;
  const changes = [data.stopped ? null : data.stop_at, data.stopped_until, data.mode?.until]
    .filter((t) => t && t > now).map((t) => (t - now) * 1000 + 500);
  return Math.min(base, ...changes);
}

// The status line leads with the stop time while one is in effect: the next event minus its lead
// time, with the event, or while stopped, when the feed returns and the events it's stopped for.
function stopNote(data) {
  if (!data.stop_at) return "";
  if (data.stopped) {
    const events = [data.ongoing_event, data.next_event].filter(Boolean).map((e) => `“${e.title}”`);
    const until = data.stopped_until ? ` until ${when(data.stopped_until)}` : "";
    return `Stopped${until}${events.length ? ` for ${events.join(", then ")}` : ""}`;
  }
  const event = data.stop_event ? ` for “${data.stop_event.title}”` : "";
  return `Stopping at ${when(data.stop_at)}${event} (${inTime(data.stop_at)}) · posts that would run past it are pushed down`;
}

let lastStop = null;  // the stop fields of the last feed response
function stopScreenShown() {
  return !!(lastStop && lastStop.stopped);
}

function renderStopNotice() {
  let notice = document.getElementById("stop-notice");
  const stopped = stopScreenShown();
  feedEl.classList.toggle("stopped", stopped);
  if (!stopped) { notice?.remove(); return; }
  if (!notice) {
    notice = el("div", { id: "stop-notice", role: "status" });
    feedEl.before(notice);
  }
  const ongoing = lastStop.ongoing_event;
  // The next event the feed is stopped for, such as lunch right after the meeting taking place:
  // in its lead time, to get ready for it.
  const upcoming = lastStop.next_event;
  // An ongoing event that caused the stop is shown below and says enough on its own.
  notice.replaceChildren(...(upcoming ? [leadNote(upcoming)] : []));
  if (ongoing) notice.append(eventDetails(ongoing, false));
  if (upcoming) notice.append(eventDetails(upcoming, true));
  tickCountdown();
}

// The lead time before an event: stop watching now and get ready for it.
function leadNote(event) {
  return `Stop watching now and get ready: “${event.title}” starts at ${when(event.calendar_start)}.`;
}

// An event in full: the one taking place now, with a countdown to its end, or the upcoming one
// to get ready for, with a countdown to its start.
function eventDetails(event, upcoming) {
  const link = /^https?:/.test(event.url || "")
    ? el("a", { href: event.url, target: "_blank", rel: "noopener noreferrer" }, "Open event ↗") : null;
  const end = upcoming ? event.calendar_start : event.deadline;
  const label = upcoming ? "starts in" : "left";
  return el("section", { class: "ongoing", "aria-label": upcoming ? "Upcoming event" : "Ongoing event" },
    el("div", { class: "source" }, [upcoming ? "Up next" : "Now", event.calendar].filter(Boolean).join(" · ")),
    el("h2", {}, event.title || "Untitled event"),
    el("div", { class: "when" }, event.deadline ? `${when(event.calendar_start)} – ${when(event.deadline)} · ` : `${when(event.calendar_start)} · `,
      el("span", { class: "countdown", "data-end": String(end), "data-label": label }, countdown(end, label))),
    event.description ? el("p", { class: "description" }, event.description) : null,
    link);
}

// Such as "1:02:03 left" or "starts in 4:05", until a unix time.
function countdown(end, label) {
  const s = Math.max(0, Math.ceil(end - Date.now() / 1000));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, "0");
  const time = `${h ? `${h}:${String(m).padStart(2, "0")}` : m}:${sec}`;
  return label === "left" ? `${time} left` : `${label} ${time}`;
}

// Updates the countdowns every second while they're shown; once one runs out (an event starts or
// ends), checks the server again so the stop screen shows what comes next (or the feed returns) without waiting
// for the next poll.
let countdownTimer = null;
const reloadedFor = new Set();
function tickCountdown() {
  clearTimeout(countdownTimer);
  const spans = [...document.querySelectorAll("#stop-notice .countdown")];
  if (!spans.length) return;
  for (const span of spans) {
    const end = Number(span.dataset.end);
    span.textContent = countdown(end, span.dataset.label);
    if (Date.now() / 1000 >= end && !reloadedFor.has(end)) {
      reloadedFor.add(end);  // once: the server's clock may lag behind
      load();
    }
  }
  countdownTimer = setTimeout(tickCountdown, 1000 - Date.now() % 1000);
}

// On the stop screen playback is stopped, and the alert sounds once for each stop the server reports.
let alertedStop = null;
function alertSound() {
  try {
    const ctx = alertSound.ctx || (alertSound.ctx = new (window.AudioContext || window.webkitAudioContext)());
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const t0 = ctx.currentTime;
    for (let i = 0; i < 3; i++) {  // three short beeps
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      osc.connect(gain);
      gain.connect(ctx.destination);
      const t = t0 + i * 0.3;
      gain.gain.setValueAtTime(0.25, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
      osc.start(t);
      osc.stop(t + 0.28);
    }
  } catch {}
}

function forceStop(data) {
  if (!stopScreenShown()) { alertedStop = null; return; }
  if (playingCard) {
    // Rebuilding the playing card replaces its player with the play button, which stops playback.
    for (const c of cards.values()) if (c.el === playingCard) {
      const replacement = card(c.item);
      showViewState(replacement, latest(c.item));
      c.el.replaceWith(replacement);
      c.el = replacement;
    }
    playingCard = null;
  }
  if (data && data.stopped) {
    const key = `${data.stop_event?.url}@${data.stop_at}`;
    if (alertedStop !== key) {
      alertedStop = key;
      alertSound();
    }
  }
}
