// The page of `watch_live.py`: it shows the session through `watch` and sends the buttons to the command.
import { watch } from "/dist/index.js";

const WORDS = {
  idle: "Bereit",
  loading: "Lädt",
  live: "Läuft",
  ended: "Beendet",
  failed: "Getrennt",
  sent: "Satz gesendet",
  cut: "Abgeschnitten",
  over: "Die Grafikkarte wird frei",
  lost: "Das Gesicht antwortet nicht mehr",
};
// What `watch` reports, in the page's words.
const WATCH_STATES = { loading: "loading", drawn: "live", ended: "ended" };

const byId = (id) => document.getElementById(id);
const [host, watchButton, speak, clear, end, file, state, stateText, note] = [
  "host", "watch", "speak", "clear", "end", "file", "state", "state-text", "note",
].map(byId);

let watching;
let over = false;

function show(name) {
  state.dataset.state = name;
  stateText.textContent = WORDS[name];
  speak.disabled = clear.disabled = name !== "live";
  watchButton.hidden = over || !(name === "idle" || name === "ended");
}

function finish(name, text) {
  over = true;
  watching?.stop();
  end.disabled = file.disabled = true;
  show(name);
  note.textContent = text;
}

async function post(path, done) {
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error(`the command answered ${response.status}`);
    return done;
  } catch {
    finish("failed", WORDS.lost);
    return undefined;
  }
}

watchButton.addEventListener("click", () => {
  watching = watch(host, "/viewers", [], (next) => show(WATCH_STATES[next]));
});
speak.addEventListener("click", async () => {
  note.textContent = (await post(`/speak/${encodeURIComponent(file.value)}`, WORDS.sent)) ?? note.textContent;
});
clear.addEventListener("click", async () => {
  note.textContent = (await post("/clear", WORDS.cut)) ?? note.textContent;
});
end.addEventListener("click", async () => {
  if ((await post("/end", WORDS.over)) !== undefined) finish("ended", WORDS.over);
});

try {
  const names = await (await fetch("/files")).json();
  file.replaceChildren(...names.map((name) => new Option(name, name)));
  file.hidden = names.length < 2;
  show("idle");
} catch {
  finish("failed", WORDS.lost);
}
