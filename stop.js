// The stop-watching timer. While it's ahead, the server leaves out posts that would run past it,
// so every post shown can be finished before the stop time; the post you're viewing stays even when
// it no longer fits. When the stop time runs out (the timer, or an event's lead time), the page
// switches to stop mode in JS: the feed is hidden behind a notice naming the event that caused the
// stop (or a generic timer message) — in an event's lead time, "stop watching now and get ready",
// with the event in full and a countdown to its start — and the event taking place now, if any, with a countdown to its end; playback is force-stopped and an audio alert sounds (see forceStop) — and since the feed
// itself is still served, switching back to the normal feed is seamless once the stop is over. The "Stop mode" button next to the timer switches between stop
// mode and the normal feed manually at any time. A time earlier today means tomorrow.
const stopForm = document.getElementById("stop-form");
const stopTime = document.getElementById("stop-time");
const stopClear = document.getElementById("stop-clear");
stopForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!stopTime.value) return;
  const [h, m] = stopTime.value.split(":").map(Number);
  const when = new Date();
  when.setHours(h, m, 0, 0);
  if (when.getTime() <= Date.now()) when.setDate(when.getDate() + 1);
  const { ok, data } = await api("/api/stop", { stop_at: when.getTime() / 1000 });
  if (!ok) return say(data.error || "Couldn't set the timer.", true);
  stopClear.hidden = false;
  say(`Stopping at ${stopTime.value}: the feed now shows only posts you can finish before then.`);
  load();
});
stopClear.addEventListener("click", async () => {
  await api("/api/stop", { stop_at: null });
  showStopTimer(null);
  say("Stop timer cleared.");
  load();
});

// The timer as saved on the server, in the box (or empty).
function showStopTimer(stopAt) {
  stopTime.value = stopAt ? clock(stopAt) : "";
  stopClear.hidden = !stopAt;
}

// While a stop time is in effect, poll faster (10s) so the stop firing — and the feed returning
// once the event is over — are noticed promptly.
function pollDelay(pending, stopAt) {
  return pending ? 1500 : stopAt ? 10000 : 30000;
}

// The status line leads with the stop time while one is in effect: the timer, or the next event
// minus its lead time, whichever comes first.
function stopNote(stopAt, stopped) {
  if (!stopAt) return "";
  const hhmm = clock(stopAt);
  if (stopped) return `Stopped at ${hhmm}`;
  const remaining = Math.round((stopAt - Date.now() / 1000) / 60);
  const when = remaining > 0 ? `in ${remaining} min` : "now";
  return `Stopping at ${hhmm} (${when}) · only posts that finish before then`;
}

let lastStop = null;  // the stop fields of the last feed response
// null follows the server's stopped flag; true or false is the Stop mode button forcing stop mode or the normal feed.
let modeOverride = null;
const stopModeBtn = document.getElementById("stop-mode");
function stopModeActive() {
  return modeOverride !== null ? modeOverride : !!(lastStop && lastStop.stopped);
}
function updateStopModeBtn() {
  const active = stopModeActive();
  stopModeBtn.textContent = active ? "Back to normal" : "Stop mode";
  stopModeBtn.setAttribute("aria-pressed", String(active));
  stopModeBtn.title = active
    ? "Switch back to the normal feed now"
    : "Switch to stop mode now: hide the feed and stop playback";
}
stopModeBtn.addEventListener("click", () => {
  modeOverride = !stopModeActive();
  forceStop(null, true);  // manual switch: stop playback too, but no audio alert
  render();
});

function renderStopNotice() {
  let notice = document.getElementById("stop-notice");
  const stopped = stopModeActive();
  feedEl.classList.toggle("stopped", stopped);
  updateStopModeBtn();
  if (!stopped) { notice?.remove(); return; }
  if (!notice) {
    notice = el("div", { id: "stop-notice", role: "status" });
    feedEl.before(notice);
  }
  const ongoing = lastStop && lastStop.ongoing_event;
  // An event that caused the stop but hasn't started yet: its lead time, to get ready for it.
  const upcoming = lastStop && lastStop.stopped && lastStop.stop_event
    && !(ongoing && lastStop.stop_event.url === ongoing.url) ? lastStop.stop_event : null;
  if (lastStop && lastStop.stopped) {
    const hhmm = clock(lastStop.stop_at);
    const clear = el("button", { type: "button" }, "Clear timer");
    clear.addEventListener("click", () => stopClear.click());
    notice.replaceChildren(
      // The ongoing event shown below is the cause: it says enough on its own.
      upcoming ? leadNote(upcoming) : lastStop.stop_event ? "" : `Stop time reached at ${hhmm}.`,
      stopClear.hidden ? null : clear);  // clearing a timer doesn't end an event's stop
  } else {
    const back = el("button", { type: "button" }, "Back to feed");
    back.addEventListener("click", () => { modeOverride = false; render(); });
    notice.replaceChildren("Stop mode is on — the feed is hidden and playback is stopped. ", back);
  }
  if (ongoing) notice.append(eventDetails(ongoing, false));
  if (upcoming) notice.append(eventDetails(upcoming, true));
  tickCountdown();
}

// The lead time before an event: stop watching now and get ready for it.
function leadNote(event) {
  return `Stop watching now and get ready: “${event.title}” starts at ${clock(event.calendar_start)}.`;
}

// An event in full: the one taking place now, with a countdown to its end, or the upcoming one
// to get ready for, with a countdown to its start.
function eventDetails(event, upcoming) {
  const day = (unix) => new Date(unix * 1000).toDateString();
  const when = (unix) => day(unix) === new Date().toDateString() ? clock(unix)
    : `${new Date(unix * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${clock(unix)}`;
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
// ends), reloads the feed so stop mode shows what comes next (or the feed returns) without waiting
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

// In stop mode cards you opened no longer keep their place, playback is stopped, and the alert
// sounds once for each stop the server reports (not for the Stop mode button).
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

function forceStop(data, manual = false) {
  if (!stopModeActive()) { alertedStop = null; return; }
  stay.clear();  // the stay behavior no longer applies while stop mode is on
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
  if (!manual && data && data.stopped) {  // alert only when the server's stop fires, not on manual switches
    const key = `${data.stop_event ? data.stop_event.url : "timer"}@${data.stop_at}`;
    if (alertedStop !== key) {
      alertedStop = key;
      alertSound();
    }
  }
}
