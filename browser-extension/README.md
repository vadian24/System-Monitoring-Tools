# System Monitor Video FPS

This optional Chromium add-on sends live playback FPS and a rolling dropped-frame count from a playing Twitch or YouTube video to System Monitor 1.3.8 or later.

1. In System Monitor, choose **Overview → Live FPS Setup → Browser video → Open add-on folder**. Extract `System-Monitor-Video-FPS.zip` to a folder you can keep. You can also [download it directly](https://raw.githubusercontent.com/vadian24/System-Monitoring-Tools/main/downloads/System-Monitor-Video-FPS.zip).
2. In Chrome, Edge, Brave, or Vivaldi, open the browser's extensions page, turn on **Developer mode**, and select **Load unpacked**. Choose the folder containing `manifest.json`.
3. In System Monitor, open **Overview → Live FPS setup → Copy video pairing code**. Open the extension popup, paste the code, and select **Pair**.
4. Reload the Twitch or YouTube tab and start playback. You can switch windows while the browser continues reporting playback frames.

To update an unpacked copy, extract the new ZIP into the existing extension folder, select **Reload** on the extension card, then reload the Twitch or YouTube tab. Keeping the same folder preserves the existing pairing.

The add-on samples the page's video playback counters and sends the site label, measured FPS, and dropped-frame count to the local dashboard at `127.0.0.1`. It sends no page URLs, titles, browsing history, or account details. Disable video FPS in System Monitor to close the receiver and expire the pairing code.
