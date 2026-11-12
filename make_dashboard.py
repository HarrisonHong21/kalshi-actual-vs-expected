"""Build the results page (docs/index.html) from the archived tables in output/.

Run:  python make_dashboard.py

The page is one HTML file with its data built in, so GitHub Pages can serve it from docs/ and
it still opens from a downloaded copy. Its only outside request is one web font, with a fallback. Every number on it is read from
output/band_table.csv, output/cost_table.csv and output/key_numbers.json and formatted the same
way study.py formats the README tables. Nothing is typed by hand; re-run this after study.py.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).parent
BANDS = ["under 5c", "5-10c", "10-20c", "20-50c", "50-80c", "80c and up"]
ALL_PRICES = "all prices"


def signed(x, digits=1):
    """Same rule as study.py: a rounded zero is shown as plain 0.0, not -0.0."""
    return f"{0:.{digits}f}" if round(x, digits) == 0 else f"{x:+.{digits}f}"


def span(low, high, unit=""):
    return f"{low * 100:+.1f}{unit} to {high * 100:+.1f}{unit}"


def pct(x, is_signed=False):
    return f"{x * 100:+.1f}%" if is_signed else f"{x * 100:.1f}%"


def read_table(name):
    with open(ROOT / "output" / name, newline="") as handle:
        return list(csv.DictReader(handle))


def band_rows():
    rows = []
    for r in read_table("band_table.csv"):
        gap, low, high = float(r["gap"]), float(r["gap_low"]), float(r["gap_high"])
        rows.append({
            "sample": r["sample"], "scans": r["scans"], "band": r["band"],
            "gap": gap * 100, "low": low * 100, "high": high * 100,
            "contracts": f"{int(r['contracts']):,}",
            "games": r["games"],
            "actual": pct(float(r["actual"])),
            "expected": pct(float(r["expected"])),
            "gap_text": f"{signed(gap * 100)} ({span(low, high)})",
            "cred_independent": f"{float(r['credibility_if_independent']):.2f}",
            "cred_by_game": f"{float(r['credibility_by_game']):.2f}",
        })
    return rows


def cost_rows():
    rows = []
    for r in read_table("cost_table.csv"):
        spread, fee, mid = float(r["lost_to_half_spread"]), float(r["lost_to_fee"]), float(r["lost_to_midpoint"])
        rows.append({
            "sample": r["sample"], "scans": r["scans"], "band": r["band"],
            "spread": spread * 100, "fee": fee * 100, "mid": mid * 100,
            "ask": f"{float(r['ask_cents']):.1f}c",
            "spread_text": pct(spread),
            "fee_text": pct(fee),
            "mid_text": f"{pct(mid, True)} ({span(float(r['lost_to_midpoint_low']), float(r['lost_to_midpoint_high']), '%')})",
            "return_short": pct(float(r["return"]), True),
            "return_text": f"{pct(float(r['return']), True)} ({span(float(r['return_low']), float(r['return_high']), '%')})",
        })
    return rows


def build():
    n = json.loads((ROOT / "output" / "key_numbers.json").read_text())
    data = {"bands": BANDS, "all_prices": ALL_PRICES, "band_rows": band_rows(), "cost_rows": cost_rows()}
    page = TEMPLATE
    for key, value in n.items():
        if isinstance(value, str):
            page = page.replace("{{" + key + "}}", value)
    page = page.replace("{{DATA}}", json.dumps(data, separators=(",", ":")))
    if "{{" in page:
        raise SystemExit("a placeholder in the template has no value in key_numbers.json")
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page)
    print(f"wrote {out / 'index.html'}")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Do Kalshi prices match what happened?</title>
<meta name="description" content="Actual versus expected for {{contracts}} settled Kalshi player-prop contracts from {{games}} games.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&display=swap">
<style>
:root {
  color-scheme: light;
  --paper: #f0f3ea; --ink: #13251f; --muted: #55645d; --field: #12382f; --chalk: #f3f6ea;
  --stripe: rgba(19, 37, 31, 0.055); --rule: rgba(19, 37, 31, 0.22);
  --spread: #1c5d4e; --fee: #9bc4ae; --mid: #e3a417;
  --sans: "Archivo", "Helvetica Neue", Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 400 17px/1.55 var(--sans); }
.wrap { max-width: 900px; margin: 0 auto; padding-inline: 20px; }
a { color: inherit; text-decoration-thickness: 2px; text-underline-offset: 3px; text-decoration-color: var(--mid); }
p { margin: 0; max-width: 66ch; }

header { background: var(--field); color: var(--chalk); padding-block: 56px 44px; }
h1 {
  margin: 0 0 18px; font-weight: 800; font-stretch: 68%; letter-spacing: -0.01em;
  font-size: clamp(2.6rem, 8.5vw, 5.2rem); line-height: 0.94; text-wrap: balance;
}
header p { font-size: 1.1rem; max-width: 56ch; color: rgba(243, 246, 234, 0.82); }
header p strong { color: var(--chalk); font-weight: 600; }

main { display: flex; flex-direction: column; gap: 56px; padding-block: 44px 72px; }
h2 { margin: 0 0 8px; font-weight: 800; font-stretch: 72%; font-size: 1.9rem; line-height: 1.05; text-wrap: balance; }
.lede { color: var(--muted); font-size: 0.95rem; }
section { display: flex; flex-direction: column; gap: 18px; }

.findings { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 28px; counter-reset: finding; }
.findings li { counter-increment: finding; font-size: 0.95rem; }
.findings li::before {
  content: counter(finding); display: block; font-weight: 800; font-stretch: 62%;
  font-size: 3rem; line-height: 1; color: var(--spread); margin-bottom: 6px;
}
.findings strong { display: block; font-size: 1.05rem; line-height: 1.3; margin-bottom: 4px; }

.controls { display: flex; flex-wrap: wrap; gap: 14px 36px; align-items: end; position: sticky; top: 0; z-index: 2; background: var(--paper); padding-block: 12px; border-bottom: 2px solid var(--ink); }
.group { display: flex; flex-direction: column; gap: 6px; }
.group > span { font-size: 0.85rem; color: var(--muted); }
.group > div { display: flex; flex-wrap: wrap; }
button.pick {
  font: 600 0.95rem/1 var(--sans); padding: 10px 14px; cursor: pointer; border-radius: 0;
  background: transparent; color: var(--ink); border: 2px solid var(--ink); margin-right: -2px;
}
button.pick:hover { background: var(--stripe); }
button.pick[aria-pressed="true"] { background: var(--ink); color: var(--paper); }
button.pick:focus-visible, summary:focus-visible, a:focus-visible { outline: 3px solid var(--mid); outline-offset: 2px; }
.note { font-size: 0.95rem; font-weight: 600; background: var(--mid); color: var(--ink); padding: 10px 14px; max-width: none; }

.chart { display: flex; flex-direction: column; font-variant-numeric: tabular-nums; }
.row { display: grid; grid-template-columns: 104px minmax(0, 1fr) 176px; gap: 14px; align-items: center; padding-block: 5px; }
.row:nth-child(odd):not(.axis-row) .track { background: var(--stripe); }
.row .label { font-weight: 700; font-stretch: 80%; font-size: 1.05rem; }
.row .value { font-size: 0.9rem; color: var(--muted); }
.row.total { border-top: 2px solid var(--ink); margin-top: 6px; padding-top: 10px; }
.row.total .value { color: var(--ink); font-weight: 700; }
.track { position: relative; height: 30px; }
.track .zero { position: absolute; top: -5px; bottom: -5px; width: 2px; margin-left: -1px; background: var(--ink); }
.track .range { position: absolute; top: 10px; height: 10px; background: var(--spread); opacity: 0.38; }
.track .dot { position: absolute; top: 7px; width: 16px; height: 16px; margin-left: -8px; border-radius: 50%; background: var(--ink); border: 3px solid var(--paper); }
.track .seg { position: absolute; top: 4px; height: 22px; }
.seg.spread { background: var(--spread); }
.seg.fee { background: var(--fee); }
.seg.mid { background: var(--mid); }
.seg.mid.helped { background: repeating-linear-gradient(135deg, var(--mid) 0 3px, var(--paper) 3px 6px); }
.axis { position: relative; height: 22px; font-size: 0.8rem; color: var(--muted); }
.axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 0.9rem; }
.legend i { display: inline-block; width: 14px; height: 14px; margin-right: 7px; vertical-align: -2px; }

.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-size: 0.9rem; font-variant-numeric: tabular-nums; width: 100%; margin-top: 10px; }
th, td { text-align: left; padding: 7px 16px 7px 0; white-space: nowrap; }
th { font-weight: 700; border-bottom: 2px solid var(--ink); }
td { border-bottom: 1px solid var(--rule); }
summary { cursor: pointer; font-weight: 600; font-size: 0.95rem; width: fit-content; }

.limits { background: var(--field); color: var(--chalk); padding-block: 44px 52px; }
.limits ul { margin: 14px 0 22px; padding-left: 1.1em; columns: 2; column-gap: 40px; }
.limits li { break-inside: avoid; margin-bottom: 10px; }
.limits p { font-size: 0.95rem; color: rgba(243, 246, 234, 0.82); max-width: none; }

@media (max-width: 680px) {
  .findings { grid-template-columns: 1fr; gap: 20px; }
  .row { grid-template-columns: 78px minmax(0, 1fr); }
  .row .value { grid-column: 2; }
  .axis-row .value { display: none; }
  .limits ul { columns: 1; }
  .controls { position: static; }
}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>Do Kalshi prices match what happened?</h1>
    <p>An actual-versus-expected study of <strong>{{contracts}}</strong> settled NFL and NBA player-prop
      contracts from <strong>{{games}}</strong> games, {{period}}. By Harrison Hong.</p>
  </div>
</header>

<main class="wrap">
  <ol class="findings">
    <li><strong>No measurable gap between prices and outcomes.</strong> Every price band paid out within
      {{pooled_gap_lowest}} to {{pooled_gap_highest}} points of what its price implied, and no band's 95% range
      excludes zero. The data cannot rule out a gap of up to {{pooled_widest_bound}} points in a band.</li>
    <li><strong>The loss was mostly the cost of trading.</strong> Buying YES on every contract at the quoted ask
      would have returned {{return_all}} (95% range {{return_all_range}}). The half-spread and the fee, both known
      before buying, were {{known_share_of_loss}} of that loss. This is a hypothetical return, not a trading record.</li>
    <li><strong>It held when any one game was dropped.</strong> Leaving out each game in turn keeps the return
      between {{return_all_without_one_game}}.</li>
  </ol>

  <div>
    <div class="controls" role="group" aria-label="Choose what the charts show">
      <div class="group"><span>League</span><div id="league"></div></div>
      <div class="group"><span>Prices logged</span><div id="scans"></div></div>
    </div>
    <p class="note" id="caveat" hidden></p>
  </div>

  <section>
    <div>
      <h2>Actual minus expected, by price band</h2>
      <p class="lede">Each dot is how much more or less often contracts paid out than their midpoint price
        implied, in percentage points. The bar is the 95% range from resampling whole games 2,000 times.
        A bar that crosses zero means no gap was found.</p>
    </div>
    <div class="chart" id="gap-chart"></div>
    <details>
      <summary>Exact numbers</summary>
      <div class="scroll"><table id="gap-table"></table></div>
    </details>
  </section>

  <section>
    <div>
      <h2>Where a YES buyer's money would have gone</h2>
      <p class="lede">Share of the money spent that would have been lost to each part, buying at the quoted
        ask and holding to settlement. The half-spread and fee are known before buying. The midpoint error is the
        only part that depends on the price being wrong.</p>
    </div>
    <div class="legend">
      <span><i style="background:var(--spread)"></i>Half-spread</span>
      <span><i style="background:var(--fee)"></i>Fee</span>
      <span><i style="background:var(--mid)"></i>Midpoint error</span>
      <span><i class="seg mid helped" style="position:static"></i>Midpoint error in the buyer's favour</span>
    </div>
    <div class="chart" id="cost-chart"></div>
    <details>
      <summary>Exact numbers</summary>
      <div class="scroll"><table id="cost-table"></table></div>
    </details>
  </section>
</main>

<div class="limits">
  <div class="wrap">
    <h2>What this does not show</h2>
    <ul>
      <li>The sample is five weeks of one season. Only the bands from 50c up reach full credibility.</li>
      <li>The games are the ones my app scanned. They are not a random sample of Kalshi.</li>
      <li>Returns are at prices on screen with an assumed order size. They are not trades, and real fills could be worse.</li>
      <li>Single-league results are exploratory. With two leagues and six bands, some ranges exclude zero by chance.</li>
      <li>This is a study of prices. It contains no picks and is not betting advice.</li>
    </ul>
    <p>Method, robustness checks, code and tests are on
      <a href="https://github.com/HarrisonHong21/kalshi-actual-vs-expected">GitHub</a>.
      There is also a
      <a href="https://github.com/HarrisonHong21/kalshi-actual-vs-expected/blob/main/one_page_summary.pdf">one-page summary (PDF)</a>.</p>
  </div>
</div>

<script>
const DATA = {{DATA}};
const LEAGUES = [["NFL + NBA", "Both"], ["NFL", "NFL"], ["NBA", "NBA"]];
const SCANS = [["all", "Any time before the game"], ["fresh", "Within 6 hours of the start"]];
const state = { sample: "NFL + NBA", scans: "all" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function makeButtons(hostId, options, key) {
  const host = document.getElementById(hostId);
  for (const [value, label] of options) {
    const button = el("button", "pick", label);
    button.type = "button";
    button.dataset.value = value;
    button.addEventListener("click", () => { state[key] = value; render(); });
    host.appendChild(button);
  }
}

function rowsFor(table, bands) {
  const picked = table.filter(r => r.sample === state.sample && r.scans === state.scans);
  return bands.map(band => picked.find(r => r.band === band)).filter(Boolean);
}

// A round axis that covers the data and always includes zero.
function axis(low, high, step) {
  const min = Math.min(0, Math.floor(low / step) * step);
  const max = Math.max(0, Math.ceil(high / step) * step);
  const ticks = [];
  for (let t = min; t <= max + 1e-9; t += step) ticks.push(Math.round(t));
  return { min, max, ticks, at: v => ((v - min) / (max - min)) * 100 };
}

function axisRow(scale, unit) {
  const row = el("div", "row axis-row");
  row.appendChild(el("div"));
  const line = el("div", "axis");
  for (const t of scale.ticks) {
    const tick = el("span", "", (t > 0 ? "+" : "") + t + unit);
    tick.style.left = scale.at(t) + "%";
    line.appendChild(tick);
  }
  row.appendChild(line);
  row.appendChild(el("div", "value"));
  return row;
}

function fillTable(id, headers, rows) {
  const table = document.getElementById(id);
  table.replaceChildren();
  const head = el("tr");
  for (const h of headers) head.appendChild(el("th", "", h));
  table.appendChild(head);
  for (const cells of rows) {
    const tr = el("tr");
    for (const c of cells) tr.appendChild(el("td", "", c));
    table.appendChild(tr);
  }
}

function renderGaps() {
  const rows = rowsFor(DATA.band_rows, DATA.bands);
  const scale = axis(Math.min(...rows.map(r => r.low)), Math.max(...rows.map(r => r.high)), 2);
  const chart = document.getElementById("gap-chart");
  chart.replaceChildren();
  for (const r of rows) {
    const row = el("div", "row");
    row.appendChild(el("div", "label", r.band));
    const track = el("div", "track");
    const zero = el("div", "zero"); zero.style.left = scale.at(0) + "%";
    const range = el("div", "range");
    range.style.left = scale.at(r.low) + "%";
    range.style.width = (scale.at(r.high) - scale.at(r.low)) + "%";
    const dot = el("div", "dot"); dot.style.left = scale.at(r.gap) + "%";
    track.append(zero, range, dot);
    row.appendChild(track);
    row.appendChild(el("div", "value", r.gap_text));
    chart.appendChild(row);
  }
  chart.appendChild(axisRow(scale, ""));
  fillTable("gap-table",
    ["Price band", "Contracts", "Paid out", "Expected", "Gap in points (95% range)", "Credibility if independent", "Credibility by game"],
    rows.map(r => [r.band, r.contracts, r.actual, r.expected, r.gap_text, r.cred_independent, r.cred_by_game]));
}

function renderCosts() {
  const rows = rowsFor(DATA.cost_rows, DATA.bands.concat([DATA.all_prices]));
  const highest = Math.max(...rows.map(r => r.spread + r.fee + Math.max(0, r.mid)));
  const lowest = Math.min(...rows.map(r => Math.min(0, r.mid)));
  const scale = axis(lowest, highest, highest - lowest > 60 ? 20 : 10);
  const chart = document.getElementById("cost-chart");
  chart.replaceChildren();
  for (const r of rows) {
    const row = el("div", r.band === DATA.all_prices ? "row total" : "row");
    row.appendChild(el("div", "label", r.band));
    const track = el("div", "track");
    const place = (className, from, to) => {
      const seg = el("div", "seg " + className);
      seg.style.left = scale.at(from) + "%";
      seg.style.width = Math.max(0, scale.at(to) - scale.at(from)) + "%";
      track.appendChild(seg);
    };
    place("spread", 0, r.spread);
    place("fee", r.spread, r.spread + r.fee);
    if (r.mid >= 0) place("mid", r.spread + r.fee, r.spread + r.fee + r.mid);
    else place("mid helped", r.mid, 0);
    const zero = el("div", "zero"); zero.style.left = scale.at(0) + "%";
    track.appendChild(zero);
    row.appendChild(track);
    row.appendChild(el("div", "value", "return " + r.return_short));
    chart.appendChild(row);
  }
  chart.appendChild(axisRow(scale, "%"));
  fillTable("cost-table",
    ["Price band", "Average ask", "Half-spread", "Fee", "Midpoint error (95% range)", "Return (95% range)"],
    rows.map(r => [r.band, r.ask, r.spread_text, r.fee_text, r.mid_text, r.return_text]));
}

function render() {
  for (const button of document.querySelectorAll("button.pick")) {
    const key = button.parentElement.id === "league" ? "sample" : "scans";
    button.setAttribute("aria-pressed", String(state[key] === button.dataset.value));
  }
  const notes = [];
  if (state.sample !== "NFL + NBA") notes.push("Single-league results are exploratory: with two leagues and six bands, some ranges will exclude zero by chance.");
  if (state.scans === "fresh") notes.push("Showing only contracts whose price was logged within 6 hours of the start. The three findings above use all contracts.");
  const caveat = document.getElementById("caveat");
  caveat.textContent = notes.join(" ");
  caveat.hidden = notes.length === 0;
  renderGaps();
  renderCosts();
}

makeButtons("league", LEAGUES, "sample");
makeButtons("scans", SCANS, "scans");
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build()
