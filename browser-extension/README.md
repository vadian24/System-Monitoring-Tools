# System Monitor Video FPS

This optional Chromium add-on sends live playback FPS and a rolling dropped-frame count from a focused Twitch or YouTube video to System Monitor 1.3.8 or later.

1. [Download the browser add-on](https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/System-Monitor-Video-FPS.zip) and extract it to a folder you can keep.
2. In Chrome, Edge, Brave, or Vivaldi, open the browser's extensions page, turn on **Developer mode**, and select **Load unpacked**. Choose the folder containing `manifest.json`.
3. In System Monitor, open **Overview → Live FPS setup → Copy video pairing code**. Open the extension popup, paste the code, and select **Pair**.
4. Reload the Twitch or YouTube tab and start playback. Keep the video tab focused. The dashboard can be visible on a second display.

The add-on samples the page's video playback counters and sends the site label, measured FPS, and dropped-frame count to the local dashboard at `127.0.0.1`. It sends no page URLs, titles, browsing history, or account details. Disable video FPS in System Monitor to close the receiver and expire the pairing code.
