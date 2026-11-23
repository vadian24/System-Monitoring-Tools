chrome.runtime.onMessage.addListener(async (sample, sender) => {
  const source = {"https://www.twitch.tv": "Twitch", "https://www.youtube.com": "YouTube"}[sender.origin];
  if (!source || !sender.tab?.active || !Number.isFinite(sample?.fps) ||
      !Number.isInteger(sample?.drops)) return;
  try {
    const window = await chrome.windows.get(sender.tab.windowId);
    const {key} = await chrome.storage.local.get("key");
    if (!window.focused || !/^[a-f0-9]{32}$/.test(key || "")) return;
    await fetch("http://127.0.0.1:43178/video", {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(2000),
      headers: {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
      body: JSON.stringify({source, fps: sample.fps, drops: sample.drops})
    });
  } catch {
    // System Monitor may be closed or video telemetry may be disabled.
  }
});
