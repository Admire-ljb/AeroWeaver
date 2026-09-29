/* Progressive enhancements for the static research project page. */
(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const panels = [...document.querySelectorAll('[role="tabpanel"]')];
  function activateTab(tab) {
    tabs.forEach(item => {
      item.setAttribute('aria-selected', String(item === tab));
      item.tabIndex = item === tab ? 0 : -1;
    });
    panels.forEach(panel => { panel.hidden = panel.id !== tab.getAttribute('aria-controls'); });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activateTab(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); activateTab(tabs[next]); tabs[next].focus(); }
    });
  });

  const dialog = document.getElementById('figure-dialog');
  const enlarged = document.getElementById('enlarged-figure');
  const original = document.getElementById('original-figure');
  if (typeof dialog.showModal === 'function') {
    document.querySelectorAll('[data-lightbox]').forEach(link => {
      link.addEventListener('click', event => {
        event.preventDefault();
        enlarged.src = link.href;
        enlarged.alt = link.querySelector('img').alt;
        original.href = link.href;
        dialog.showModal();
        document.body.style.overflow = 'hidden';
      });
    });
    document.getElementById('close-figure').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', event => {
      const box = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) dialog.close();
    });
    dialog.addEventListener('close', () => { document.body.style.overflow = ''; });
  }

  const videos = [...document.querySelectorAll('video')];
  const mediaState = new Map(videos.map(video => [video, {time: null, fallback: false}]));
  const connection = navigator.connection;
  const saveData = connection?.saveData || /(^|-)2g$/.test(connection?.effectiveType || '');

  // The anonymous host does not support byte ranges. Preserve attached media
  // when pausing so resuming never depends on seeking after video.load().
  function suspend(video) {
    video.pause();
    video.preload = 'none';
  }
  function prioritize(video) {
    videos.forEach(other => { if (other !== video) suspend(other); });
    video.preload = 'auto';
  }
  function changeQuality(video, quality) {
    const state = mediaState.get(video);
    const playing = !video.paused;
    state.time ??= video.currentTime;
    const efficient = video.dataset.av1 && video.canPlayType('video/webm; codecs="av01.0.05M.08"');
    video.src = quality === '1080' ? video.dataset.hd :
      (efficient && !state.fallback ? video.dataset.av1 : video.dataset.sd);
    video.preload = 'auto';
    video.load();
    prioritize(video);
    if (playing) video.play().catch(() => {});
  }

  videos.forEach(video => {
    const state = mediaState.get(video);
    const toolbar = document.createElement('div');
    toolbar.className = 'video-options';
    const status = document.createElement('span');
    status.className = 'video-status';
    status.setAttribute('role', 'status');
    toolbar.append(status);
    state.status = status;
    if (video.dataset.hd) {
      const download = document.createElement('a');
      download.href = video.dataset.hd;
      download.setAttribute('download', '');
      download.textContent = 'Download 1080p ↓';
      toolbar.append(download);
      if (video.dataset.av1 && video.canPlayType('video/webm; codecs="av01.0.05M.08"')) {
        video.src = video.dataset.av1;
      }
    }
    video.closest('.video-shell').after(toolbar);
    video.addEventListener('loadedmetadata', () => {
      if (state.time !== null) {
        video.currentTime = Math.min(state.time, video.duration || state.time);
        state.time = null;
      }
    });
    video.addEventListener('play', () => prioritize(video));
    video.addEventListener('waiting', () => { status.textContent = 'Buffering…'; });
    video.addEventListener('playing', () => { status.textContent = ''; });
    video.addEventListener('canplay', () => { status.textContent = ''; });
    video.addEventListener('pause', () => { status.textContent = ''; });
    video.addEventListener('error', () => {
      // Some browsers report AV1 support but cannot decode a particular stream.
      if (video.currentSrc.endsWith('.webm') && !state.fallback) {
        state.fallback = true;
        changeQuality(video, '720');
      } else {
        status.textContent = 'Video could not load. Please try again.';
      }
    });
    // Pointer/focus intent takes precedence over speculative viewport loading.
    video.addEventListener('pointerenter', () => { if (videos.every(v => v.paused)) prioritize(video); });
    video.addEventListener('focus', () => { if (videos.every(v => v.paused)) prioritize(video); });
  });

  const teaser = document.getElementById('teaser');
  let teaserStarted = false;
  if ('IntersectionObserver' in window) {
    const teaserObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting && !reducedMotion.matches && !saveData && !teaserStarted && videos.every(video => video === teaser || video.paused)) {
          teaserStarted = true;
          teaser.play().catch(() => {});
        } else if (!entry.isIntersecting) suspend(teaser);
      }
    }, {threshold: .35});
    teaserObserver.observe(teaser);
    // Warm only the nearest player. Avoid six simultaneous video transfers.
    if (!saveData) {
      const nearby = new Set();
      let warmTimer;
      const preloadObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) nearby.add(entry.target);
          else { nearby.delete(entry.target); if (entry.target.paused) suspend(entry.target); }
        });
        clearTimeout(warmTimer);
        warmTimer = setTimeout(() => {
          if (document.hidden || videos.some(video => !video.paused)) return;
          const nearest = [...nearby].sort((a, b) =>
            Math.abs(a.getBoundingClientRect().top - innerHeight / 2) -
            Math.abs(b.getBoundingClientRect().top - innerHeight / 2))[0];
          if (nearest) prioritize(nearest);
        }, 200);
      }, {rootMargin: '250px 0px'});
      videos.forEach(video => preloadObserver.observe(video));
    }
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) videos.forEach(suspend); });

})();
