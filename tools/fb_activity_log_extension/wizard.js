/* global FB_ACTIVITY_LOG_URLS */

const EXPORT_KEYS = ['fbcExport_comments', 'fbcExport_posts'];

const FALLBACK_ACTIVITY_LOG_URLS = {
  comments: 'https://www.facebook.com/me/allactivity',
  posts: 'https://www.facebook.com/me/allactivity',
};

function defaultActivityLogUrls() {
  if (typeof FB_ACTIVITY_LOG_URLS !== 'undefined' && FB_ACTIVITY_LOG_URLS) {
    return FB_ACTIVITY_LOG_URLS;
  }
  return FALLBACK_ACTIVITY_LOG_URLS;
}

function setStatus(text, isErr) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = 'status' + (isErr ? ' err' : '');
}

function updateStepIndicator(activeStepNum) {
  document.querySelectorAll('.step-dot').forEach((dot) => {
    const n = parseInt(dot.getAttribute('data-step'), 10);
    dot.classList.remove('active', 'done');
    if (dot.classList.contains('skipped')) return;
    if (n < activeStepNum) dot.classList.add('done');
    else if (n === activeStepNum) dot.classList.add('active');
  });
}

function markSkippedSteps(mode) {
  const nav = document.querySelector('.step-indicator');
  if (!nav) return;
  nav.querySelectorAll('.step-dot, .step-connector').forEach((el) => el.classList.remove('skipped'));

  const skipped = new Set();
  if (!mode.comments) { skipped.add(2); skipped.add(3); }
  if (!mode.posts) { skipped.add(4); skipped.add(5); }

  nav.querySelectorAll('.step-dot').forEach((dot) => {
    if (skipped.has(parseInt(dot.getAttribute('data-step'), 10))) dot.classList.add('skipped');
  });

  // Fade connectors adjacent to skipped dots
  const children = Array.from(nav.children);
  children.forEach((el, i) => {
    if (!el.classList.contains('step-connector')) return;
    if (children[i - 1]?.classList.contains('skipped') || children[i + 1]?.classList.contains('skipped')) {
      el.classList.add('skipped');
    }
  });
}

function showStep(id) {
  document.querySelectorAll('.step').forEach((s) => s.classList.add('hidden'));
  const el = document.getElementById(id);
  if (el) {
    el.classList.remove('hidden');
    const stepNum = parseInt(el.getAttribute('data-step-num'), 10);
    if (!isNaN(stepNum)) updateStepIndicator(stepNum);
  }
}

function getMode() {
  return document.getElementById('mode').value === 'full' ? 'full' : 'quick';
}

function getDiagnosticMode() {
  return document.getElementById('diagnostic-mode')?.checked ?? false;
}

function getHarvestMode() {
  return {
    comments: document.getElementById('harvest-comments')?.checked ?? true,
    posts: document.getElementById('harvest-posts')?.checked ?? true,
  };
}

/** Wizard caps: 0 = unlimited for comments/posts; 0 for images/videos = built-in defaults in content script. */
function getCaps() {
  const num = (id) => {
    const el = document.getElementById(id);
    if (!el) return 0;
    const x = parseInt(String(el.value).trim(), 10);
    return Number.isFinite(x) && x >= 0 ? x : 0;
  };
  const allowTa = document.getElementById('media-allowlist');
  const mediaAllowlistUrls =
    allowTa && allowTa.value.trim()
      ? allowTa.value
          .split('\n')
          .map((l) => l.trim())
          .filter(Boolean)
      : [];
  return {
    maxComments: num('cap-comments'),
    maxPosts: num('cap-posts'),
    maxImages: num('cap-images'),
    maxVideos: num('cap-videos'),
    mediaAllowlistUrls,
    useTabExtraction: document.getElementById('use-tab-extraction')?.checked ?? true,
  };
}

async function persistCaps() {
  await chrome.storage.local.set({ fbcExport_caps: getCaps() });
}

async function loadCapFields() {
  const r = await chrome.storage.local.get(['fbcExport_caps']);
  const c = r.fbcExport_caps;
  if (!c || typeof c !== 'object') return;
  const apply = (id, key) => {
    const el = document.getElementById(id);
    if (!el) return;
    const v = c[key];
    if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
      el.value = String(v);
    }
  };
  apply('cap-comments', 'maxComments');
  apply('cap-posts', 'maxPosts');
  apply('cap-images', 'maxImages');
  apply('cap-videos', 'maxVideos');
  if (Array.isArray(c.mediaAllowlistUrls) && c.mediaAllowlistUrls.length) {
    const ta = document.getElementById('media-allowlist');
    if (ta) ta.value = c.mediaAllowlistUrls.join('\n');
  }
  const tabEx = document.getElementById('use-tab-extraction');
  if (tabEx && typeof c.useTabExtraction === 'boolean') {
    tabEx.checked = c.useTabExtraction;
  }
}

const WIZARD_PREFS_KEY = 'fbcExport_wizard_prefs';

function getWizardPrefsPayload() {
  return {
    scrollMode: getMode(),
    harvestComments: document.getElementById('harvest-comments')?.checked ?? true,
    commentsOwnPostsOnly: document.getElementById('comments-own-posts-only')?.checked ?? false,
    harvestPosts: document.getElementById('harvest-posts')?.checked ?? true,
    skipMedia: document.getElementById('skip-media')?.checked ?? false,
    diagnosticMode: document.getElementById('diagnostic-mode')?.checked ?? false,
  };
}

async function persistWizardPrefs() {
  await chrome.storage.local.set({ [WIZARD_PREFS_KEY]: getWizardPrefsPayload() });
}

async function loadWizardPrefs() {
  const r = await chrome.storage.local.get([WIZARD_PREFS_KEY]);
  const p = r[WIZARD_PREFS_KEY];
  if (!p || typeof p !== 'object') return;
  const applyBool = (id, key) => {
    const el = document.getElementById(id);
    if (!el || typeof p[key] !== 'boolean') return;
    el.checked = p[key];
  };
  const modeEl = document.getElementById('mode');
  if (modeEl && (p.scrollMode === 'quick' || p.scrollMode === 'full')) {
    modeEl.value = p.scrollMode;
  }
  applyBool('harvest-comments', 'harvestComments');
  applyBool('comments-own-posts-only', 'commentsOwnPostsOnly');
  applyBool('harvest-posts', 'harvestPosts');
  applyBool('skip-media', 'skipMedia');
  applyBool('diagnostic-mode', 'diagnosticMode');
}

function normalizeSearch(urlStr) {
  try {
    const u = new URL(urlStr);
    const keys = [...new Set([...u.searchParams.keys()])].sort();
    const sp = new URLSearchParams();
    for (const k of keys) {
      for (const v of u.searchParams.getAll(k)) {
        sp.append(k, v);
      }
    }
    const q = sp.toString();
    return `${u.origin}${u.pathname.replace(/\/$/, '') || '/'}${q ? `?${q}` : ''}`;
  } catch {
    return String(urlStr).split('#')[0];
  }
}

function isFacebookUrl(url) {
  try {
    const h = new URL(url).hostname.toLowerCase();
    return h === 'www.facebook.com' || h === 'facebook.com' || h.endsWith('.facebook.com');
  } catch {
    return false;
  }
}

function isActivityLogUrl(url) {
  if (!url || !isFacebookUrl(url)) return false;
  return url.includes('allactivity');
}

function urlsMatchNavTarget(tabUrl, targetUrl) {
  if (!tabUrl || !targetUrl) return false;
  try {
    return normalizeSearch(tabUrl) === normalizeSearch(targetUrl);
  } catch {
    return false;
  }
}

async function loadUrlFields() {
  const d = defaultActivityLogUrls();
  const defaults = {
    comments: d.comments,
    posts: d.posts,
  };
  const r = await chrome.storage.local.get(['fbCustomUrls']);
  document.getElementById('url-comments').value = r.fbCustomUrls?.comments || defaults.comments;
  document.getElementById('url-posts').value = r.fbCustomUrls?.posts || defaults.posts;
}

async function getNavUrls() {
  const d = defaultActivityLogUrls();
  const r = await chrome.storage.local.get(['fbCustomUrls']);
  return {
    comments: (r.fbCustomUrls?.comments || d.comments).trim(),
    posts: (r.fbCustomUrls?.posts || d.posts).trim(),
  };
}

async function saveUrlFields() {
  await chrome.storage.local.set({
    fbCustomUrls: {
      comments: document.getElementById('url-comments').value.trim(),
      posts: document.getElementById('url-posts').value.trim(),
    },
  });
  setStatus('URLs saved.');
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function waitTabComplete(tabId) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Page load timed out — try again or reload the tab.'));
    }, 90000);

    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        const settleMs = 1200 + Math.floor(Math.random() * 2400);
        setTimeout(resolve, settleMs);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function navigateTab(url) {
  const tab = await getActiveTab();
  if (!tab?.id) {
    throw new Error('No active tab.');
  }
  const tabId = tab.id;
  const done = waitTabComplete(tabId);
  await chrome.tabs.update(tabId, { url });
  await done;
}

async function sendPhase(phase, opts = {}) {
  const tab = await getActiveTab();
  if (!tab?.id) {
    throw new Error('No active tab.');
  }
  await persistCaps();
  const caps = getCaps();
  return chrome.tabs.sendMessage(tab.id, { type: 'RUN_PHASE', phase, mode: getMode(), caps, diagnosticEnabled: getDiagnosticMode(), ...opts });
}

/** Polls `fbcExport_zip_progress` from content script during ZIP / media fetch. */
function startZipProgressPolling(statusUpdater) {
  const bar = document.getElementById('zip-progress');
  if (bar) bar.classList.remove('hidden');

  const interval = setInterval(async () => {
    try {
      const r = await chrome.storage.local.get(['fbcExport_zip_progress']);
      const p = r.fbcExport_zip_progress;
      if (!p) return;
      const t0 = p.startedAt || p.updatedAt || Date.now();
      const elapsedSec = Math.round((Date.now() - t0) / 1000);
      const elapsedStr =
        elapsedSec >= 60 ? `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s` : `${elapsedSec}s`;

      let msg = '';
      let pct = 0;

      if (p.stage === 'merge') {
        msg = `Preparing export — ${p.detail || 'merging harvest'} (${elapsedStr})`;
        pct = 6;
      } else if (p.stage === 'enrich') {
        const m = p.enrichMax ?? 0;
        const a = p.enrichAttempt ?? 0;
        const found = p.imagesFound ?? 0;
        const cap = p.maxOgByCap ?? m;
        if (m === 0) {
          msg = `No per-post image extraction needed… ${elapsedStr}`;
          pct = 42;
        } else if (a === 0) {
          msg = `Opening post pages for images — ${m} to fetch… ${elapsedStr}`;
          pct = 12;
        } else {
          msg = `Fetching post pages for images: ${a}/${m} opened, ${found} found… ${elapsedStr}`;
          pct = 10 + Math.min(34, Math.round((a / Math.max(1, m)) * 34));
        }
      } else if (p.stage === 'download') {
        if (p.skipped) {
          msg = `${p.detail || 'Skipping downloads'} (${elapsedStr})`;
          pct = 85;
        } else {
          const tot = p.total ?? 0;
          const done = p.completed ?? 0;
          const ok = p.ok ?? 0;
          const err = p.err ?? 0;
          msg =
            tot === 0
              ? `No media to download — packaging… ${elapsedStr}`
              : `Downloading media: ${done}/${tot} (${ok} saved${err ? `, ${err} failed` : ''})… ${elapsedStr}`;
          pct = tot === 0 ? 88 : 46 + Math.min(46, Math.round((done / Math.max(1, tot)) * 46));
        }
      } else if (p.stage === 'zip_build' || p.stage === 'metadata') {
        // v2.8.0 streams JSON to disk instead of packaging a ZIP.
        const fallback = p.stage === 'metadata' ? 'Saving metadata…' : 'Building ZIP…';
        msg = `${p.detail || fallback} (${elapsedStr})`;
        pct = 96;
      } else {
        msg = `Working… (${elapsedStr})`;
        pct = 8;
      }

      if (bar) bar.value = Math.min(99, pct);
      statusUpdater(msg);
    } catch (_e) {
      /* storage read failure — ignore */
    }
  }, 750);

  return () => {
    clearInterval(interval);
    if (bar) {
      bar.value = 100;
      setTimeout(() => bar.classList.add('hidden'), 600);
    }
  };
}

function startProgressPolling(progressBarId, statusUpdater) {
  const maxRoundsEstimate = getMode() === 'full' ? 1200 : 40;
  const bar = document.getElementById(progressBarId);
  if (bar) bar.classList.remove('hidden');

  const interval = setInterval(async () => {
    try {
      const r = await chrome.storage.local.get(['fbcExport_progress']);
      const p = r.fbcExport_progress;
      if (!p) return;
      const elapsed = Math.round((p.elapsed || 0) / 1000);
      const elapsedMin = Math.floor(elapsed / 60);
      const elapsedSec = elapsed % 60;
      const elapsedStr = elapsedMin > 0 ? `${elapsedMin}m ${elapsedSec}s` : `${elapsedSec}s`;
      const pct = Math.min(99, Math.round((p.rounds / maxRoundsEstimate) * 100));
      if (bar) bar.value = pct;
      const itemLabel = p.phase === 'comments' ? 'comments' : 'post links';
      statusUpdater(`Scrolling for ${itemLabel}: ${p.totalItems} found (${elapsedStr})`);
    } catch (_e) {
      // storage read failure — ignore
    }
  }, 2500);

  return () => {
    clearInterval(interval);
    if (bar) {
      bar.value = 100;
      setTimeout(() => bar.classList.add('hidden'), 1200);
    }
  };
}

function harvestSummaryText(d) {
  const phase = d.phase;
  const count = phase === 'comments' ? d.commentsWithTextCount : d.postsWithTextCount;
  const noun = phase === 'comments' ? 'comment' : 'post link';
  const nouns = count === 1 ? noun : noun + 's';
  const capped = d.mediaCapped ? ' (media cap hit — some URLs skipped)' : '';

  if (d.stoppedBecause === 'capComments') {
    const lim = d.caps?.maxComments ?? '?';
    return `✓ Reached max comments (${lim}) — captured ${count} ${nouns}.${capped}`;
  }
  if (d.stoppedBecause === 'capPosts') {
    const lim = d.caps?.maxPosts ?? '?';
    return `✓ Reached max posts (${lim}) — captured ${count} ${nouns}.${capped}`;
  }

  if (d.stoppedBecause === 'user') {
    return `⚠ Stopped early — captured ${count} ${nouns} up to this point.${capped}`;
  }
  if (d.stoppedBecause === 'scrollStable' || d.stoppedBecause === 'itemStable') {
    return `✓ Found ${count} ${nouns}. Reached the end of your Activity Log.${capped}`;
  }
  return `✓ Found ${count} ${nouns} (scroll limit reached — run Full mode for complete history).${capped}`;
}

async function showExportSummary() {
  const r = await chrome.storage.local.get(['fbcExport_comments', 'fbcExport_posts']);
  const comments = r.fbcExport_comments;
  const posts = r.fbcExport_posts;
  const postCount = posts?.postsWithTextCount ?? 0;
  const commentCount = comments?.commentsWithTextCount ?? 0;
  const mediaCount = [
    ...(posts?.mediaCandidates ?? []),
    ...(comments?.mediaCandidates ?? []),
  ].length;

  const summary = document.getElementById('export-summary');
  if (!summary) return;
  summary.textContent = `Ready to export: ${postCount.toLocaleString()} post links, ${commentCount.toLocaleString()} comments, ~${mediaCount.toLocaleString()} media items`;
  summary.classList.remove('hidden');
}

async function sendStop() {
  const tab = await getActiveTab();
  if (!tab?.id) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: 'STOP_PHASE' });
  } catch (_e) {
    /* content script may not be injected */
  }
}

// ── Harvest functions ────────────────────────────────────────────────────────

async function autoZipAndDownload() {
  showStep('step-zip');
  await showExportSummary().catch(() => {});
  const skipMedia = document.getElementById('skip-media')?.checked || false;
  const stopZipPoll = startZipProgressPolling((msg) => setStatus(msg));
  const finishBtn = document.getElementById('btn-finish-now');
  finishBtn.classList.remove('hidden');
  finishBtn.disabled = false;
  setStatus(
    skipMedia
      ? 'Saving JSON metadata (skipping media)…'
      : 'Streaming media to disk — slow stages can be cut short with "Finish now".',
  );

  try {
    const res = await sendPhase('media_zip', { skipMedia });
    if (!res?.ok) {
      setStatus(res?.error || 'Export failed', true);
      document.getElementById('btn-zip').classList.remove('hidden');
      return;
    }
    const d = res.data;
    const stoppedEarly = d?.stoppedEarly === true;
    if (skipMedia) {
      setStatus('✓ Metadata saved (media skipped). Run the Python extractor to import posts.');
    } else {
      const written = d.mediaFilesWritten;
      const errors = d.mediaErrorsCount;
      const prefix = stoppedEarly ? '✓ Finished early — ' : '✓ Export complete — ';
      const msg =
        errors > 0
          ? `${prefix}${written} media files saved, ${errors} failed (see media_errors.json).`
          : `${prefix}${written} media files saved.`;
      setStatus(msg);
    }
    showStep('step-done');
  } catch (e) {
    setStatus(String(e), true);
    document.getElementById('btn-zip').classList.remove('hidden');
  } finally {
    finishBtn.classList.add('hidden');
    finishBtn.disabled = true;
    stopZipPoll();
  }
}

async function startPostsHarvest() {
  const harvestBtn = document.getElementById('btn-harvest-posts');
  const stopBtn = document.getElementById('btn-stop-posts');

  harvestBtn.classList.add('hidden');
  setStatus('Harvesting posts… (keep Activity Log tab focused in this window)');
  stopBtn.disabled = false;

  const stopPolling = startProgressPolling('harvest-posts-progress', (msg) => setStatus(msg));
  try {
    const res = await sendPhase('posts');
    stopPolling();
    stopBtn.disabled = true;
    if (!res?.ok) {
      setStatus(res?.error || 'Harvest failed', true);
      harvestBtn.classList.remove('hidden');
      return;
    }
    setStatus(harvestSummaryText(res.data));
    await autoZipAndDownload();
  } catch (e) {
    stopPolling();
    stopBtn.disabled = true;
    harvestBtn.classList.remove('hidden');
    setStatus(String(e), true);
  }
}

async function startCommentsHarvest() {
  const harvestBtn = document.getElementById('btn-harvest-comments');
  const stopBtn = document.getElementById('btn-stop');

  harvestBtn.classList.add('hidden');
  setStatus('Harvesting comments… (keep Activity Log tab focused in this window)');
  stopBtn.disabled = false;

  const stopPolling = startProgressPolling('harvest-comments-progress', (msg) => setStatus(msg));
  try {
    const res = await sendPhase('comments', { commentsOwnPostsOnly: getWizardPrefsPayload().commentsOwnPostsOnly });
    stopPolling();
    stopBtn.disabled = true;
    if (!res?.ok) {
      setStatus(res?.error || 'Harvest failed', true);
      harvestBtn.classList.remove('hidden');
      return;
    }
    setStatus(harvestSummaryText(res.data));
    // Auto-proceed based on what the user wanted to harvest
    const mode = getHarvestMode();
    if (mode.posts) {
      await proceedToPostsFlow(true);
    } else {
      await autoZipAndDownload();
    }
  } catch (e) {
    stopPolling();
    stopBtn.disabled = true;
    harvestBtn.classList.remove('hidden');
    setStatus(
      'Cannot run on this page. Reload the Activity Log tab after installing/updating the extension, then retry.\n' +
        String(e),
      true,
    );
  }
}

// ── Navigation helpers ───────────────────────────────────────────────────────

async function goToHarvestCommentsFromNav() {
  showStep('step-harvest-comments');
  document.getElementById('btn-harvest-comments').classList.remove('hidden');
  document.getElementById('btn-stop').disabled = true;
  setStatus('On Comments Activity Log — start harvest when ready.');
}

async function updateNavCommentsStepUi() {
  const tab = await getActiveTab();
  const urls = await getNavUrls();
  const skipNote = document.getElementById('nav-comments-skip-note');
  const skipBtn = document.getElementById('btn-skip-comments-harvest');
  const hint = document.getElementById('nav-comments-hint');

  if (tab?.url && urlsMatchNavTarget(tab.url, urls.comments)) {
    await goToHarvestCommentsFromNav();
    return;
  }

  skipNote.classList.add('hidden');
  skipBtn.classList.add('hidden');
  hint.textContent = 'Opens your Activity Log with the Comments filter (best-effort URL).';

  if (tab?.url && isActivityLogUrl(tab.url)) {
    // Double-quoted, no nested quotes, no "Go" — avoids stale cached wizard.js parse errors.
    skipNote.textContent =
      "Activity Log filter does not match Comments. Use the primary button on this step, or start harvest if the comments list is already shown.";
    skipNote.classList.remove('hidden');
    skipBtn.classList.remove('hidden');
  }
}

async function proceedIntroToCommentsFlow() {
  const tab = await getActiveTab();
  const urls = await getNavUrls();

  // Already on the right URL — skip navigation
  if (tab?.url && urlsMatchNavTarget(tab.url, urls.comments)) {
    showStep('step-harvest-comments');
    setStatus('Already on your Comments Activity Log — starting harvest…');
    await startCommentsHarvest();
    return;
  }

  // Auto-navigate
  showStep('step-nav-comments');
  setStatus('Navigating to Comments Activity Log…');
  try {
    await navigateTab(urls.comments);
    showStep('step-harvest-comments');
    setStatus('On Comments Activity Log — starting harvest…');
    await startCommentsHarvest();
  } catch (e) {
    setStatus(String(e), true);
    // Fall back to manual nav UI so user can try manually
    await updateNavCommentsStepUi();
  }
}

async function goToHarvestPostsFromNav() {
  showStep('step-harvest-posts');
  document.getElementById('btn-harvest-posts').classList.remove('hidden');
  document.getElementById('btn-stop-posts').disabled = true;
  setStatus('On Posts Activity Log — start harvest when ready.');
}

async function updateNavPostsStepUi() {
  const tab = await getActiveTab();
  const urls = await getNavUrls();
  const skipNote = document.getElementById('nav-posts-skip-note');
  const skipBtn = document.getElementById('btn-skip-posts-harvest');

  if (tab?.url && urlsMatchNavTarget(tab.url, urls.posts)) {
    await goToHarvestPostsFromNav();
    return;
  }

  skipNote.classList.add('hidden');
  skipBtn.classList.add('hidden');

  if (tab?.url && isActivityLogUrl(tab.url)) {
    skipNote.textContent =
      "Activity Log filter does not match Posts. Use the primary button on this step, or start harvest if the posts list is already shown.";
    skipNote.classList.remove('hidden');
    skipBtn.classList.remove('hidden');
  }
}

async function proceedToPostsFlow(autoStart = false) {
  const tab = await getActiveTab();
  const urls = await getNavUrls();

  // Already on the right URL
  if (tab?.url && urlsMatchNavTarget(tab.url, urls.posts)) {
    showStep('step-harvest-posts');
    setStatus(
      autoStart
        ? 'On Posts Activity Log — starting harvest…'
        : 'Already on your Posts Activity Log — start harvest when ready.',
    );
    if (autoStart) await startPostsHarvest();
    return;
  }

  if (autoStart) {
    showStep('step-nav-posts');
    setStatus('Navigating to Posts Activity Log…');
    try {
      await navigateTab(urls.posts);
      showStep('step-harvest-posts');
      setStatus('On Posts Activity Log — starting harvest…');
      await startPostsHarvest();
    } catch (e) {
      setStatus(String(e), true);
      await updateNavPostsStepUi();
    }
    return;
  }

  showStep('step-nav-posts');
  setStatus('Navigate to your Posts Activity Log, then start harvest.');
  await updateNavPostsStepUi();
}

// ── UI binding ───────────────────────────────────────────────────────────────

function bindUi() {
  document.getElementById('save-urls').addEventListener('click', () => {
    saveUrlFields().catch((e) => setStatus(String(e), true));
  });

  document.getElementById('btn-start-wizard').addEventListener('click', () => {
    const mode = getHarvestMode();
    if (!mode.comments && !mode.posts) {
      setStatus('Select at least one item to harvest.', true);
      return;
    }
    markSkippedSteps(mode);
    if (mode.comments) {
      proceedIntroToCommentsFlow().catch((e) => setStatus(String(e), true));
    } else {
      proceedToPostsFlow(true).catch((e) => setStatus(String(e), true));
    }
  });

  document.getElementById('btn-back-intro').addEventListener('click', () => {
    showStep('step-intro');
    setStatus('');
  });

  document.getElementById('btn-nav-comments').addEventListener('click', async () => {
    setStatus('Navigating…');
    try {
      const tab = await getActiveTab();
      const urls = await getNavUrls();
      if (tab?.url && urlsMatchNavTarget(tab.url, urls.comments)) {
        await goToHarvestCommentsFromNav();
        return;
      }
      await navigateTab(urls.comments);
      await goToHarvestCommentsFromNav();
    } catch (e) {
      setStatus(String(e), true);
    }
  });

  document.getElementById('btn-skip-comments-harvest').addEventListener('click', () => {
    goToHarvestCommentsFromNav();
  });

  const stopComments = document.getElementById('btn-stop');

  document.getElementById('btn-harvest-comments').addEventListener('click', async () => {
    await startCommentsHarvest();
  });

  stopComments.addEventListener('click', () => {
    sendStop();
    setStatus('Stop requested — harvest will finish after current scroll round.');
  });

  document.getElementById('btn-nav-posts').addEventListener('click', async () => {
    setStatus('Navigating…');
    try {
      const tab = await getActiveTab();
      const urls = await getNavUrls();
      if (tab?.url && urlsMatchNavTarget(tab.url, urls.posts)) {
        await goToHarvestPostsFromNav();
        return;
      }
      await navigateTab(urls.posts);
      await goToHarvestPostsFromNav();
    } catch (e) {
      setStatus(String(e), true);
    }
  });

  document.getElementById('btn-skip-posts-harvest').addEventListener('click', () => {
    goToHarvestPostsFromNav();
  });

  const stopPosts = document.getElementById('btn-stop-posts');

  document.getElementById('btn-harvest-posts').addEventListener('click', async () => {
    await startPostsHarvest();
  });

  stopPosts.addEventListener('click', () => {
    sendStop();
    setStatus('Stop requested — harvest will finish after current scroll round.');
  });

  // Manual retry button — only visible after autoZipAndDownload fails
  document.getElementById('btn-zip').addEventListener('click', async () => {
    document.getElementById('btn-zip').classList.add('hidden');
    await autoZipAndDownload();
  });

  // Cut the media-fetch / enrichment phase short and save whatever has been
  // collected so far. Sends STOP_PHASE, which sets the content-script token's
  // `cancelled` flag — enrichment + media download pool both bail at their next
  // cancellation checkpoint, and runMediaAndZipInner falls through to its
  // streaming-save section unconditionally (writes posts.json + media files
  // collected up to that point to disk via chrome.downloads.download).
  document.getElementById('btn-finish-now').addEventListener('click', async () => {
    const btn = document.getElementById('btn-finish-now');
    btn.disabled = true;
    btn.textContent = 'Finishing…';
    setStatus('Finishing early — saving what has been collected so far.');
    await sendStop();
  });

  document.getElementById('btn-reset').addEventListener('click', async () => {
    await chrome.storage.local.remove(EXPORT_KEYS);
    markSkippedSteps({ comments: true, posts: true });
    showStep('step-intro');
    setStatus('');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadUrlFields().catch(() => {});
  loadCapFields()
    .then(() => persistCaps())
    .catch(() => {});
  loadWizardPrefs()
    .then(() => persistWizardPrefs())
    .catch(() => {});
  bindUi();
  showStep('step-intro');

  ['cap-comments', 'cap-posts', 'cap-images', 'cap-videos', 'use-tab-extraction'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('change', () => {
        persistCaps().catch(() => {});
      });
    }
  });

  const allowTa = document.getElementById('media-allowlist');
  if (allowTa) {
    allowTa.addEventListener('change', () => {
      persistCaps().catch(() => {});
    });
  }

  ['mode', 'harvest-comments', 'harvest-posts', 'skip-media', 'diagnostic-mode', 'comments-own-posts-only'].forEach(
    (id) => {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('change', () => {
          persistWizardPrefs().catch(() => {});
        });
      }
    },
  );

  // Disable "only on my own posts" sub-option when comments harvest is off
  const harvestCommentsEl = document.getElementById('harvest-comments');
  const ownPostsEl = document.getElementById('comments-own-posts-only');
  const ownPostsLabel = document.getElementById('label-comments-own-posts-only');
  function syncOwnPostsVisibility() {
    if (!harvestCommentsEl || !ownPostsLabel) return;
    ownPostsLabel.style.opacity = harvestCommentsEl.checked ? '1' : '0.4';
    if (ownPostsEl) ownPostsEl.disabled = !harvestCommentsEl.checked;
  }
  if (harvestCommentsEl) {
    harvestCommentsEl.addEventListener('change', syncOwnPostsVisibility);
    syncOwnPostsVisibility();
  }
});
