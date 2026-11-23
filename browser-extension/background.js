const sites = {"https://www.twitch.tv": "Twitch", "https://www.youtube.com": "YouTube"};
const playing = new Map();
let selected;
let previous;

chrome.tabs.onRemoved.addListener(tab => playing.delete(tab));

chrome.runtime.onMessage.addListener(async (sample, sender) => {
  const source = sites[sender.origin];
  const tab = sender.tab?.id;
  if (!source || !Number.isInteger(tab)) return;

  const now = performance.now();
  for (const [id, reading] of playing) {
    if (now - reading.time > 4000) playing.delete(id);
  }
  if (sample?.inactive) {
    playing.delete(tab);
  } else if (Number.isFinite(sample?.fps) && Number.isInteger(sample?.drops)) {
    playing.set(tab, {sample, source, time: now});
  } else {
    return;
  }

  // Prefer focused playback, otherwise keep the selected video while it reports.
  const focused = [...playing].find(([, reading]) => reading.sample.focused);
  const visible = [...playing].find(([, reading]) => reading.sample.visible);
  selected = focused?.[0] ?? (playing.has(selected) ? selected : visible?.[0] ?? playing.keys().next().value);
  const reading = playing.get(selected);
  if (!reading || reading === previous) return;
  previous = reading;

  try {
    const {key} = await chrome.storage.local.get("key");
    if (!/^[a-f0-9]{32}$/.test(key || "")) return;
    await fetch("http://127.0.0.1:43178/video", {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(2000),
      headers: {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
      body: JSON.stringify({source: reading.source, fps: reading.sample.fps, drops: reading.sample.drops})
    });
  } catch {
    // System Monitor may be closed or video telemetry may be disabled.
  }
});
