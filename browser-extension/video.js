(() => {
  let video = null;
  let previous = null;
  let history = [];
  const report = sample => chrome.runtime.sendMessage(sample).catch(() => {});

  setInterval(() => {
    const player = [...document.querySelectorAll("video")]
      .filter(item => !item.paused && !item.ended && item.getClientRects().length &&
        typeof item.getVideoPlaybackQuality === "function")
      .sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    if (!player) {
      if (video) report({inactive: true});
      video = null;
      previous = null;
      history = [];
      return;
    }
    const {totalVideoFrames: total, droppedVideoFrames: dropped} = player.getVideoPlaybackQuality();
    const time = performance.now() / 1000;
    if (player !== video || !previous || total < previous.total || dropped < previous.dropped ||
        time - previous.time > 60) {
      video = player;
      previous = {total, dropped, time};
      history = [];
      return;
    }
    const drops = dropped - previous.dropped;
    const fps = Math.max(0, total - previous.total - drops) / (time - previous.time);
    history.push({time, drops});
    history = history.filter(point => point.time > time - 60);
    previous = {total, dropped, time};
    report({
      fps,
      drops: history.reduce((sum, point) => sum + point.drops, 0),
      visible: !document.hidden,
      focused: document.hasFocus()
    });
  }, 1000);
})();
