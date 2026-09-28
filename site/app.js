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

  const mainVideo = document.getElementById('main-video');
  const chapters = [...document.querySelectorAll('[data-time]')];
  const chapterStatus = document.querySelector('.chapter-status');
  let pendingChapter = null;
  mainVideo.addEventListener('loadedmetadata', () => {
    if (pendingChapter !== null) {
      mainVideo.currentTime = pendingChapter;
      pendingChapter = null;
    }
  });
  chapters.forEach(button => button.addEventListener('click', async () => {
    const time = Number(button.dataset.time);
    if (mainVideo.readyState >= 1) mainVideo.currentTime = time;
    else pendingChapter = time;
    mainVideo.scrollIntoView({behavior: reducedMotion.matches ? 'auto' : 'smooth', block: 'center'});
    try { await mainVideo.play(); }
    catch { chapterStatus.textContent = 'Chapter selected. Press play on the video to continue.'; }
  }));
  mainVideo.addEventListener('timeupdate', () => {
    const active = chapters.findLastIndex(button => Number(button.dataset.time) <= mainVideo.currentTime);
    chapters.forEach((button, index) => {
      button.classList.toggle('active', index === active);
      if (index === active) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
  });

  const videos = [...document.querySelectorAll('video')];
  videos.forEach(video => {
    video.addEventListener('play', () => {
      videos.forEach(other => { if (other !== video && !other.paused) other.pause(); });
    });
  });

  const teaser = document.getElementById('teaser');
  let teaserStarted = false;
  if ('IntersectionObserver' in window) {
    const teaserObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting && !reducedMotion.matches && !teaserStarted && videos.every(video => video === teaser || video.paused)) {
          teaserStarted = true;
          teaser.play().catch(() => {});
        } else if (!entry.isIntersecting) teaser.pause();
      }
    }, {threshold: .35});
    teaserObserver.observe(teaser);

  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) videos.forEach(video => video.pause()); });

  const copyButton = document.getElementById('copy-citation');
  copyButton.addEventListener('click', async () => {
    const citation = document.getElementById('bibtex').textContent;
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(citation);
      copyButton.textContent = 'Copied!';
      status.textContent = 'Citation copied to clipboard.';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(document.getElementById('bibtex'));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copyButton.textContent = 'Citation selected';
      status.textContent = 'Please press Ctrl+C or Command+C to copy the selected citation.';
    }
    setTimeout(() => { copyButton.textContent = 'Copy citation'; }, 2500);
  });
})();
