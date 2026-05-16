const EXPORT_KEYS = ['xExport_tweets', 'xExport_replies'];

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

function getHarvestMode() {
  return {
    tweets: document.getElementById('harvest-tweets')?.checked ?? true,
    replies: document.getElementById('harvest-replies')?.checked ?? false,
  };
}

function getCaps() {
  const num = (id) => {
    const el = document.getElementById(id);
    if (!el) return 0;
    const x = parseInt(String(el.value).trim(), 10);
    return Number.isFinite(x) && x >= 0 ? x : 0;
  };
  return {
    maxTweets: num('cap-tweets'),
    maxImages: num('cap-images'),
    maxVideos: num('cap-videos'),
  };
}

async function persistCaps() {
  await chrome.storage.local.set({ xExport_caps: getCaps() });
}

async function loadCapFields() {
  const r = await chrome.storage.local.get(['xExport_caps']);
  const c = r.xExport_caps;
  if (!c || typeof c !== 'object') return;
  const apply = (id, key) => {
    const el = document.getElementById(id);
    if (!el) return;
    const v = c[key];
    if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
      el.value = String(v);
    }
  };
  apply('cap-tweets', 'maxTweets');
  apply('cap-images', 'maxImages');
  apply('cap-videos', 'maxVideos');
}

const WIZARD_PREFS_KEY = 'xExport_wizard_prefs';

function getWizardPrefsPayload() {
  return {
    scrollMode: getMode(),
    harvestTweets: document.getElementById('harvest-tweets')?.checked ?? true,
    harvestReplies: document.getElementById('harvest-replies')?.checked ?? false,
    skipMedia: document.getElementById('skip-media')?.checked ?? false,
    username: normalizeUsername(document.getElementById('profile-username')?.value || ''),
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
  applyBool('harvest-tweets', 'harvestTweets');
  applyBool('harvest-replies', 'harvestReplies');
  applyBool('skip-media', 'skipMedia');
  const unameEl = document.getElementById('profile-username');
  if (unameEl && typeof p.username === 'string' && p.username) {
    unameEl.value = p.username;
  }
}

/**
 * Normalize a username: strip @, whitespace, surrounding slashes, and any
 * accidentally-pasted URL. Returns '' if nothing sensible is left.
 * X usernames are [A-Za-z0-9_]{1,15}.
 */
function normalizeUsername(raw) {
  let s = String(raw || '').trim();
  if (!s) return '';
  // Handle pasted URL: extract the first path segment.
  if (/^https?:\/\//i.test(s)) {
    try {
      const u = new URL(s);
      s = u.pathname.split('/').filter(Boolean)[0] || '';
    } catch { /* fall through */ }
  }
  s = s.replace(/^@+/, '').replace(/^\/+|\/+$/g, '').trim();
  if (!/^[A-Za-z0-9_]{1,15}$/.test(s)) return '';
  return s;
}

function usernameFromCurrentTabUrl(url) {
  if (!url) return '';
  try {
    const u = new URL(url);
    const h = u.hostname.toLowerCase();
    if (h !== 'x.com' && h !== 'twitter.com' && h !== 'www.x.com' && h !== 'www.twitter.com') return '';
    const parts = u.pathname.split('/').filter(Boolean);
    if (parts.length === 0) return '';
    const reserved = ['home', 'explore', 'search', 'notifications', 'messages', 'settings', 'i', 'compose', 'hashtag', 'intent'];
    const cand = parts[0].replace(/^@+/, '');
    if (reserved.includes(cand.toLowerCase())) return '';
    return /^[A-Za-z0-9_]{1,15}$/.test(cand) ? cand : '';
  } catch {
    return '';
  }
}

function isXProfileUrl(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    const h = u.hostname.toLowerCase();
    if (h !== 'x.com' && h !== 'twitter.com' && h !== 'www.x.com' && h !== 'www.twitter.com') return false;
    const parts = u.pathname.split('/').filter(Boolean);
    // Profile page: /@user or /user (1 path segment, no special routes)
    if (parts.length === 0) return false;
    if (parts.length > 2) return false;
    const reserved = ['home', 'explore', 'search', 'notifications', 'messages', 'settings', 'i', 'compose', 'hashtag', 'intent'];
    if (reserved.includes(parts[0].toLowerCase())) return false;
    // /user or /user/with_replies or /user/media or /user/likes
    return true;
  } catch {
    return false;
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function waitTabComplete(tabId) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Page load timed out.'));
    }, 60000);

    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        setTimeout(resolve, 2000);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function navigateTab(url) {
  const tab = await getActiveTab();
  if (!tab?.id) throw new Error('No active tab.');
  const done = waitTabComplete(tab.id);
  await chrome.tabs.update(tab.id, { url });
  await done;
}

async function sendPhase(phase, opts = {}) {
  const tab = await getActiveTab();
  if (!tab?.id) throw new Error('No active tab.');
  await persistCaps();
  const caps = getCaps();
  return chrome.tabs.sendMessage(tab.id, {
    type: 'RUN_PHASE',
    phase,
    mode: getMode(),
    caps,
    ...opts,
  });
}

function startZipProgressPolling(statusUpdater) {
  const bar = document.getElementById('zip-progress');
  if (bar) bar.classList.remove('hidden');

  const interval = setInterval(async () => {
    try {
      const r = await chrome.storage.local.get(['xExport_zip_progress']);
      const p = r.xExport_zip_progress;
      if (!p) return;
      const t0 = p.startedAt || p.updatedAt || Date.now();
      const elapsedSec = Math.round((Date.now() - t0) / 1000);
      const elapsedStr =
        elapsedSec >= 60 ? `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s` : `${elapsedSec}s`;

      let msg = '';
      let pct = 0;

      if (p.stage === 'merge') {
        msg = `Preparing export... (${elapsedStr})`;
        pct = 6;
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
              ? `No media to download... ${elapsedStr}`
              : `Downloading media: ${done}/${tot} (${ok} saved${err ? `, ${err} failed` : ''})... ${elapsedStr}`;
          pct = tot === 0 ? 88 : 10 + Math.min(80, Math.round((done / Math.max(1, tot)) * 80));
        }
      } else if (p.stage === 'zip_build') {
        msg = `${p.detail || 'Building ZIP...'} (${elapsedStr})`;
        pct = 96;
      } else {
        msg = `Working... (${elapsedStr})`;
        pct = 8;
      }

      if (bar) bar.value = Math.min(99, pct);
      statusUpdater(msg);
    } catch (_e) {
      /* storage read failure */
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
  const maxRoundsEstimate = getMode() === 'full' ? 1200 : 60;
  const bar = document.getElementById(progressBarId);
  if (bar) bar.classList.remove('hidden');

  const interval = setInterval(async () => {
    try {
      const r = await chrome.storage.local.get(['xExport_progress']);
      const p = r.xExport_progress;
      if (!p) return;
      const elapsed = Math.round((p.elapsed || 0) / 1000);
      const elapsedMin = Math.floor(elapsed / 60);
      const elapsedSec = elapsed % 60;
      const elapsedStr = elapsedMin > 0 ? `${elapsedMin}m ${elapsedSec}s` : `${elapsedSec}s`;
      const pct = Math.min(99, Math.round((p.rounds / maxRoundsEstimate) * 100));
      if (bar) bar.value = pct;
      statusUpdater(`Scrolling: ${p.totalItems} tweets found (${elapsedStr})`);
    } catch (_e) {
      // ignore
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
  const count = d.postsWithTextCount ?? 0;
  const noun = count === 1 ? 'tweet' : 'tweets';

  if (d.stoppedBecause === 'capTweets') {
    const lim = d.caps?.maxTweets ?? '?';
    return `Done: reached max tweets (${lim}) - captured ${count} ${noun}.`;
  }
  if (d.stoppedBecause === 'user') {
    return `Stopped early - captured ${count} ${noun}.`;
  }
  if (d.stoppedBecause === 'scrollStable' || d.stoppedBecause === 'itemStable') {
    return `Done: found ${count} ${noun}. Reached the end of the timeline.`;
  }
  return `Done: found ${count} ${noun} (scroll limit reached - run Full mode for complete history).`;
}

async function showExportSummary() {
  const r = await chrome.storage.local.get(['xExport_tweets', 'xExport_replies']);
  const tweets = r.xExport_tweets;
  const replies = r.xExport_replies;
  const tweetCount = tweets?.postsWithTextCount ?? 0;
  const replyCount = replies?.postsWithTextCount ?? 0;
  const mediaCount = [
    ...(tweets?.mediaCandidates ?? []),
    ...(replies?.mediaCandidates ?? []),
  ].length;

  const summary = document.getElementById('export-summary');
  if (!summary) return;
  const parts = [`${tweetCount.toLocaleString()} tweets`];
  if (replyCount > 0) parts.push(`${replyCount.toLocaleString()} replies`);
  parts.push(`~${mediaCount.toLocaleString()} media items`);
  summary.textContent = `Ready to export: ${parts.join(', ')}`;
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

// ---- Harvest flow ----

async function autoZipAndDownload() {
  showStep('step-zip');
  await showExportSummary().catch(() => {});
  const skipMedia = document.getElementById('skip-media')?.checked || false;
  const stopZipPoll = startZipProgressPolling((msg) => setStatus(msg));
  setStatus(skipMedia ? 'Building ZIP (skipping media)...' : 'Building ZIP - fetching media...');

  try {
    const res = await sendPhase('media_zip', { skipMedia });
    if (!res?.ok) {
      setStatus(res?.error || 'ZIP failed', true);
      document.getElementById('btn-zip').classList.remove('hidden');
      return;
    }
    const d = res.data;
    if (skipMedia) {
      setStatus('ZIP downloaded (media skipped). Run the Python extractor to import.');
    } else {
      const written = d.mediaFilesWritten;
      const errors = d.mediaErrorsCount;
      const msg =
        errors > 0
          ? `ZIP downloaded - ${written} media files saved, ${errors} failed (see media_errors.json).`
          : `ZIP downloaded - ${written} media files saved.`;
      setStatus(msg);
    }
    showStep('step-done');
  } catch (e) {
    setStatus(String(e), true);
    document.getElementById('btn-zip').classList.remove('hidden');
  } finally {
    stopZipPoll();
  }
}

async function runHarvestPhase(phase, storageKey) {
  const harvestBtn = document.getElementById('btn-harvest');
  const stopBtn = document.getElementById('btn-stop');

  harvestBtn.classList.add('hidden');
  setStatus(`Harvesting ${phase}... (keep the X tab focused)`);
  stopBtn.disabled = false;

  const stopPolling = startProgressPolling('harvest-progress', (msg) => setStatus(msg));
  try {
    const res = await sendPhase(phase);
    stopPolling();
    stopBtn.disabled = true;
    if (!res?.ok) {
      setStatus(res?.error || 'Harvest failed', true);
      harvestBtn.classList.remove('hidden');
      return false;
    }
    setStatus(harvestSummaryText(res.data));
    return true;
  } catch (e) {
    stopPolling();
    stopBtn.disabled = true;
    harvestBtn.classList.remove('hidden');
    setStatus(
      'Cannot run on this page. Reload the X tab after installing the extension, then retry.\n' + String(e),
      true,
    );
    return false;
  }
}

async function startFullHarvest() {
  const mode = getHarvestMode();

  // Harvest tweets
  if (mode.tweets) {
    const ok = await runHarvestPhase('tweets', 'xExport_tweets');
    if (!ok) return;
  }

  // Harvest replies (navigate to /with_replies tab)
  if (mode.replies) {
    const tab = await getActiveTab();
    if (tab?.url) {
      try {
        const u = new URL(tab.url);
        const parts = u.pathname.split('/').filter(Boolean);
        if (parts.length >= 1 && !parts.includes('with_replies')) {
          const repliesUrl = `${u.origin}/${parts[0]}/with_replies`;
          setStatus('Navigating to Replies tab...');
          await navigateTab(repliesUrl);
          // Short delay for React hydration
          await new Promise((r) => setTimeout(r, 2000));
        }
      } catch (_e) { /* stay on current page */ }
    }
    const ok = await runHarvestPhase('replies', 'xExport_replies');
    if (!ok) return;
  }

  // Build ZIP
  await autoZipAndDownload();
}

// ---- UI binding ----

function bindUi() {
  document.getElementById('btn-start-wizard').addEventListener('click', () => {
    const mode = getHarvestMode();
    if (!mode.tweets && !mode.replies) {
      setStatus('Select at least one item to harvest.', true);
      return;
    }
    markSkippedSteps(mode);

    // Check if already on a profile page
    getActiveTab().then((tab) => {
      if (tab?.url && isXProfileUrl(tab.url)) {
        // Already on profile - skip navigation
        showStep('step-harvest');
        setStatus('On your X profile - starting harvest...');
        startFullHarvest().catch((e) => setStatus(String(e), true));
      } else {
        showStep('step-navigate');
        setStatus('Navigate to your X profile page.');
      }
    });
  });

  document.getElementById('btn-back-intro').addEventListener('click', () => {
    showStep('step-intro');
    setStatus('');
  });

  document.getElementById('btn-nav-profile').addEventListener('click', async () => {
    const unameInput = document.getElementById('profile-username');
    const username = normalizeUsername(unameInput?.value || '');
    if (!username) {
      setStatus('Enter a valid X username (letters, digits, underscore; up to 15 chars).', true);
      return;
    }
    if (unameInput) unameInput.value = username;
    await persistWizardPrefs().catch(() => {});
    const url = `https://x.com/${username}`;
    setStatus('Navigating...');
    try {
      await navigateTab(url);
      showStep('step-harvest');
      setStatus('On profile - starting harvest...');
      await startFullHarvest();
    } catch (e) {
      setStatus(String(e), true);
    }
  });

  document.getElementById('btn-skip-nav').addEventListener('click', () => {
    showStep('step-harvest');
    setStatus('Starting harvest...');
    startFullHarvest().catch((e) => setStatus(String(e), true));
  });

  document.getElementById('btn-harvest').addEventListener('click', () => {
    startFullHarvest().catch((e) => setStatus(String(e), true));
  });

  document.getElementById('btn-stop').addEventListener('click', () => {
    sendStop();
    setStatus('Stop requested - harvest will finish after current scroll round.');
  });

  document.getElementById('btn-zip').addEventListener('click', async () => {
    document.getElementById('btn-zip').classList.add('hidden');
    await autoZipAndDownload();
  });

  document.getElementById('btn-reset').addEventListener('click', async () => {
    await chrome.storage.local.remove(EXPORT_KEYS);
    showStep('step-intro');
    setStatus('');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadCapFields().then(() => persistCaps()).catch(() => {});
  loadWizardPrefs().then(() => persistWizardPrefs()).catch(() => {});
  bindUi();
  showStep('step-intro');

  ['cap-tweets', 'cap-images', 'cap-videos'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => persistCaps().catch(() => {}));
  });

  ['mode', 'harvest-tweets', 'harvest-replies', 'skip-media'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => persistWizardPrefs().catch(() => {}));
  });

  // Pre-fill username from current tab (only if persisted pref didn't already set it).
  getActiveTab().then((tab) => {
    const unameEl = document.getElementById('profile-username');
    if (!unameEl || unameEl.value) return;
    const fromUrl = usernameFromCurrentTabUrl(tab?.url || '');
    if (fromUrl) unameEl.value = fromUrl;
  });

  // Persist username as the user types (debounced by change event).
  const unameEl = document.getElementById('profile-username');
  if (unameEl) unameEl.addEventListener('change', () => persistWizardPrefs().catch(() => {}));
});
