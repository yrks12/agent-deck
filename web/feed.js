/* Agent Deck — /explain feed.
   100+ runs, so nothing preloads: a card is a poster until you click it, then
   it swaps in a real <video>. Otherwise the page would pull ~1.2 GB on open. */

const listEl = document.getElementById("list");
const searchEl = document.getElementById("search");
const subEl = document.getElementById("sub");

const counts = {
  runs: document.getElementById("count-runs"),
  size: document.getElementById("count-size"),
  mins: document.getElementById("count-mins"),
};

let runs = [];
let filter = "";

function setText(node, value) {
  const next = value == null ? "" : String(value);
  if (node.textContent !== next) node.textContent = next;
}

function humanBytes(n) {
  if (!n) return "0";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "GB";
  if (n >= 1e6) return Math.round(n / 1e6) + "MB";
  return Math.round(n / 1e3) + "kB";
}

function clock(seconds) {
  if (!seconds) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function when(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const days = Math.floor((Date.now() - d) / 86400000);
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  if (days === 0) return "today " + time;
  if (days === 1) return "yesterday " + time;
  if (days < 7) return days + "d ago";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function build(run) {
  const el = document.createElement("article");
  el.className = "run";
  el.innerHTML = `
    <div class="run__stage">
      <button class="run__play" type="button" aria-label="play">▶</button>
    </div>
    <div class="run__body">
      <div class="run__top">
        <h3 class="run__title"></h3>
        <span class="run__when"></span>
      </div>
      <div class="run__meta"></div>
      <p class="run__script"></p>
      <div class="run__foot">
        <button class="run__more" type="button">full script</button>
        <a class="run__audio" download>audio ↓</a>
      </div>
    </div>`;

  el.querySelector(".run__title").textContent = run.title;
  el.querySelector(".run__when").textContent = when(run.created_at);
  el.querySelector(".run__meta").textContent = [
    clock(run.duration),
    humanBytes(run.bytes),
    run.words ? run.words + " words" : "",
  ]
    .filter(Boolean)
    .join("  ·  ");

  const script = el.querySelector(".run__script");
  script.textContent = run.script || "(no script recorded)";

  const more = el.querySelector(".run__more");
  more.addEventListener("click", () => {
    const open = script.classList.toggle("run__script--open");
    more.textContent = open ? "collapse" : "full script";
  });

  const audio = el.querySelector(".run__audio");
  if (run.has_audio) audio.href = `/media/${encodeURIComponent(run.slug)}/audio`;
  else audio.remove();

  const stage = el.querySelector(".run__stage");
  const play = el.querySelector(".run__play");
  if (!run.has_video) {
    play.disabled = true;
    play.textContent = "audio only";
  } else {
    play.addEventListener("click", () => {
      const video = document.createElement("video");
      video.controls = true;
      video.autoplay = true;
      video.preload = "auto";
      video.src = `/media/${encodeURIComponent(run.slug)}/video`;
      stage.replaceChildren(video);
    });
  }

  return el;
}

function render() {
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? runs.filter(
        (r) =>
          r.title.toLowerCase().includes(needle) ||
          (r.script || "").toLowerCase().includes(needle)
      )
    : runs;

  listEl.replaceChildren(...shown.map(build));

  if (!shown.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = needle ? "nothing matches" : "no explainers yet";
    listEl.appendChild(empty);
  }
  setText(subEl, needle ? `${shown.length}/${runs.length} matching` : "/explain output");
}

async function load() {
  try {
    const res = await fetch("/api/media");
    const data = await res.json();
    runs = data.runs || [];
    const totalSeconds = runs.reduce((a, r) => a + (r.duration || 0), 0);
    setText(counts.runs.querySelector("b"), runs.length);
    setText(counts.size.querySelector("b"), humanBytes(data.summary.bytes));
    setText(counts.mins.querySelector("b"), Math.round(totalSeconds / 60));
    render();
  } catch (err) {
    setText(subEl, "could not load: " + err.message);
  }
}

searchEl.addEventListener("input", () => {
  filter = searchEl.value;
  render();
});

load();
setInterval(load, 30000);
