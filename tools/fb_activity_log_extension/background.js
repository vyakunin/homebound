// Synchronous import: makes `extractMediaFromHydratedTab` available on
// globalThis so chrome.scripting.executeScript({func: …}) can serialise it
// for the page world. Path is relative to background.js (the SW entry point).
importScripts('lib/extract_media.js');

/**
 * Opens the wizard in the side panel when the toolbar icon is clicked so it
 * stays visible while the Facebook tab navigates or reloads.
 */
function registerSidePanelClickOpensPanel() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
}

chrome.runtime.onInstalled.addListener(registerSidePanelClickOpensPanel);
registerSidePanelClickOpensPanel();

// ── Export window ─────────────────────────────────────────────────────────────
// All background tabs opened for media extraction are created in a dedicated
// minimized window so they never steal focus from the user's active tabs.
//
// MV3 service workers are evicted after ~30s idle. If extraction completes
// during one SW lifetime and the window is closed in another, the in-memory
// `_exportWindowId` would be lost and the window orphaned. To survive eviction,
// the id is mirrored to chrome.storage.session (per-browser-session, cleared
// on browser restart). On every SW startup we reconcile: read the stored id,
// verify the window still exists, and close it if no extraction is in progress.
let _exportWindowId = null;
const EXPORT_WINDOW_STORAGE_KEY = 'fbExportWindowId';

async function setExportWindowId(id) {
  _exportWindowId = id;
  try {
    if (id == null) {
      await chrome.storage.session.remove(EXPORT_WINDOW_STORAGE_KEY);
    } else {
      await chrome.storage.session.set({ [EXPORT_WINDOW_STORAGE_KEY]: id });
    }
  } catch (_) {}
}

async function loadStoredExportWindowId() {
  try {
    const obj = await chrome.storage.session.get(EXPORT_WINDOW_STORAGE_KEY);
    const id = obj?.[EXPORT_WINDOW_STORAGE_KEY];
    return typeof id === 'number' ? id : null;
  } catch {
    return null;
  }
}

/**
 * Called once per service worker startup. If a previous SW left an export
 * window behind (eviction mid-extraction, browser closed mid-export, etc.),
 * close it now so we don't accumulate orphaned minimized windows.
 */
async function reconcileOrphanedExportWindow() {
  const stored = await loadStoredExportWindowId();
  if (stored == null) return;
  try {
    await chrome.windows.get(stored);
    // Window still exists from a previous SW lifetime → close it.
    await chrome.windows.remove(stored).catch(() => {});
  } catch {
    // Already gone — just clear the stored id.
  }
  await setExportWindowId(null);
}

reconcileOrphanedExportWindow();

// If the user (or anything else) closes the export window externally, drop our
// reference so the next extraction creates a fresh one rather than failing.
chrome.windows.onRemoved.addListener((closedId) => {
  if (closedId === _exportWindowId) {
    setExportWindowId(null);
  }
});

async function getOrCreateExportWindow() {
  if (_exportWindowId !== null) {
    try {
      await chrome.windows.get(_exportWindowId);
      return _exportWindowId;
    } catch {
      await setExportWindowId(null);
    }
  }
  const win = await chrome.windows.create({
    url: 'about:blank',
    state: 'minimized',
    focused: false,
  });
  await setExportWindowId(win.id);
  return _exportWindowId;
}

/**
 * Idempotent close: removes the export window (if it exists) and clears the
 * persisted id. Safe to call from any code path, including finally blocks.
 */
async function closeExportWindowIfExists() {
  const id = _exportWindowId;
  if (id == null) return;
  await setExportWindowId(null);
  try {
    await chrome.windows.remove(id);
  } catch (_) {}
}

/** Only https facebook.com hosts — avoid open relay from content scripts. */
function isAllowedFacebookPermalinkUrl(urlStr) {
  try {
    const u = new URL(urlStr);
    if (u.protocol !== 'https:') return false;
    const h = u.hostname.toLowerCase();
    return h === 'facebook.com' || h.endsWith('.facebook.com');
  } catch {
    return false;
  }
}

/**
 * Content scripts run in the page origin (www.facebook.com). Fetching m/mbasic is cross-site
 * and fails CORS. Service worker fetch with host_permissions is not subject to that check.
 */
const MAX_PERMALINK_HTML_MESSAGE_CHARS = 6_500_000;

// ── Diagnostic webRequest capture ────────────────────────────────────────────
// Per-tab capture: tabId → array of {url, statusCode, timeStamp}.
const _diagnosticWebRequestLogByTab = {};

chrome.webRequest.onCompleted.addListener(
  (details) => {
    const tabId = details.tabId;
    if (tabId < 0) return;
    const log = _diagnosticWebRequestLogByTab[tabId];
    if (!log) return;
    if (log.length >= 500) return;
    log.push({
      url: details.url,
      statusCode: details.statusCode,
      timeStamp: details.timeStamp,
    });
  },
  { urls: ['*://*.fbcdn.net/*'] },
);

// ── Tab-based media extraction helpers ───────────────────────────────────────

/** Waits for a tab to reach status=complete or rejects on timeout. */
function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('tab_load_timeout'));
    }, timeoutMs);

    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/**
 * Injected into MAIN world immediately after tab creation to patch fetch/XHR.
 * Must have no outer-scope closures (serialised by chrome.scripting).
 * Stores every requested URL in window.__fbFetchLog for later reading.
 */
function monitorFetchesInTab() {
  window.__fbFetchLog = [];
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function(input, init) {
      try {
        const url = typeof input === 'string' ? input
          : (typeof Request !== 'undefined' && input instanceof Request ? input.url : String(input));
        window.__fbFetchLog.push(url);
      } catch (_) {}
      return origFetch.apply(this, arguments);
    };
  }
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    try { if (url) window.__fbFetchLog.push(String(url)); } catch (_) {}
    return origOpen.apply(this, arguments);
  };
}

// ── Message listener ──────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  // ── Fetch text via service worker (bypasses CORS for m/mbasic) ──
  if (msg.type === 'FB_EXPORT_FETCH_TEXT') {
    const url = msg.url;
    const timeoutMs = typeof msg.timeoutMs === 'number' ? msg.timeoutMs : 20000;
    if (!isAllowedFacebookPermalinkUrl(url)) {
      sendResponse({ ok: false, error: 'disallowed URL' });
      return false;
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    fetch(url, {
      credentials: 'include',
      mode: 'cors',
      redirect: 'follow',
      signal: ctrl.signal,
    })
      .then(async (res) => {
        clearTimeout(t);
        let text = await res.text();
        if (text.length > MAX_PERMALINK_HTML_MESSAGE_CHARS) {
          text = text.slice(0, MAX_PERMALINK_HTML_MESSAGE_CHARS);
        }
        sendResponse({ ok: res.ok, status: res.status, text });
      })
      .catch((e) => {
        clearTimeout(t);
        sendResponse({ ok: false, error: String(e) });
      });
    return true;
  }

  // ── Tab-based extraction: open post in background tab, extract after hydration ──
  if (msg.type === 'FB_EXPORT_TAB_EXTRACT') {
    const url = msg.url;
    if (!isAllowedFacebookPermalinkUrl(url)) {
      sendResponse({ ok: false, error: 'disallowed URL' });
      return false;
    }
    (async () => {
      let tabId = null;
      try {
        const attachHtmlDump = !!(msg.attachHtmlDump);

        // Create the tab in a dedicated minimized window so it never steals
        // focus from the user's active tabs.
        const exportWindowId = await getOrCreateExportWindow();
        const tab = await chrome.tabs.create({ url, active: false, windowId: exportWindowId });
        tabId = tab.id;

        // Start per-tab network capture for this tab
        _diagnosticWebRequestLogByTab[tabId] = [];

        // Inject fetch/XHR monitor at document_start by listening for the first 'loading' status.
        // injectImmediately:true after tabs.create races with page scripts; onUpdated fires earlier.
        await new Promise((resolve) => {
          function onUpdated(id, info) {
            if (id !== tabId || info.status !== 'loading') return;
            chrome.tabs.onUpdated.removeListener(onUpdated);
            chrome.scripting.executeScript({
              target: { tabId: id },
              world: 'MAIN',
              injectImmediately: true,
              func: monitorFetchesInTab,
            }).then(resolve).catch(resolve);
          }
          chrome.tabs.onUpdated.addListener(onUpdated);
          // Fallback: if tab is already past 'loading' (rare), resolve immediately
          chrome.tabs.get(tabId).then((t) => { if (t.status !== 'loading') resolve(); }).catch(resolve);
        });

        // Wait for page load + initial JS hydration
        await waitForTabComplete(tabId, 15000);
        await new Promise((r) => setTimeout(r, 4000));

        // Briefly activate the tab within the minimized export window so
        // viewport-based lazy-loaders (reel video src, intersection observers) fire.
        // This doesn't affect the user's focused window.
        await chrome.tabs.update(tabId, { active: true });
        // 6s: enough for intersection observer to fire, video fetch to start AND complete
        await new Promise((r) => setTimeout(r, 6000));

        // Check if the tab URL changed (redirect to login, marketplace, etc.)
        const tabInfo = await chrome.tabs.get(tabId);
        const finalUrl = tabInfo?.url || '';
        const tabStatus = tabInfo?.status || '';

        let results;
        let scriptError = null;
        try {
          results = await chrome.scripting.executeScript({
            target: { tabId },
            func: extractMediaFromHydratedTab,
            args: [attachHtmlDump],
          });
        } catch (scriptErr) {
          scriptError = String(scriptErr);
          results = [];
        }

        // Collect per-tab network log and clean up
        const networkLog = _diagnosticWebRequestLogByTab[tabId] ?? [];
        delete _diagnosticWebRequestLogByTab[tabId];

        // Extract the numeric video ID from the permalink (reel/video) to filter out
        // autoplay/recommended videos that also load in the background tab.
        const videoIdMatch = url.match(/\/(?:reel|videos?)\/(\d+)/);
        const expectedVideoId = videoIdMatch ? videoIdMatch[1] : null;

        function efgMatchesVideoId(cdnUrl, videoId) {
          if (!videoId) return true; // no ID to filter by → accept all
          try {
            const efg = new URL(cdnUrl).searchParams.get('efg');
            if (!efg) return true; // no efg param → can't verify, accept
            const meta = JSON.parse(atob(efg.replace(/-/g, '+').replace(/_/g, '/')));
            return String(meta.video_id) === videoId;
          } catch (_) { return true; }
        }

        // Extract video CDN URLs from network requests (MSE loads via fetch; DASH on scontent*.mp4)
        const seenVideoUrls = new Set();
        const videoCdnUrls = [];
        for (const entry of networkLog) {
          if (entry.statusCode !== 200) continue;
          const l = entry.url.toLowerCase();
          if (!l.includes('fbcdn.net')) continue;
          const isVideo = l.includes('video-') || l.includes('video.') ||
            (l.includes('scontent') && l.includes('.mp4'));
          if (!isVideo) continue;
          if (l.includes('/rsrc.php/') || l.includes('static.xx.fbcdn.net')) continue;
          if (!efgMatchesVideoId(entry.url, expectedVideoId)) continue;
          // Strip bytestart/byteend to get the base file URL
          let baseUrl = entry.url;
          try { const u = new URL(entry.url); u.searchParams.delete('bytestart'); u.searchParams.delete('byteend'); baseUrl = u.toString(); } catch(_) {}
          const dedupKey = baseUrl.split('?')[0];
          if (seenVideoUrls.has(dedupKey)) continue;
          seenVideoUrls.add(dedupKey);
          videoCdnUrls.push(baseUrl);
          if (videoCdnUrls.length >= 3) break;
        }

        const result = results?.[0]?.result ?? { urls: [], postContentUrls: [], reactionCount: 0, linkAttachments: [], mainHtml: null, debug: null };
        const domUrls = Array.isArray(result) ? result : (result.urls ?? []);
        const postContentUrls = Array.isArray(result) ? [] : (result.postContentUrls ?? []);
        const reactionCount = Array.isArray(result) ? 0 : (result.reactionCount ?? 0);
        const linkAttachments = Array.isArray(result) ? [] : (result.linkAttachments ?? []);
        const mainHtml = Array.isArray(result) ? null : (result.mainHtml ?? null);
        const tabDebug = Array.isArray(result) ? null : (result.debug ?? null);
        if (tabDebug) tabDebug.videoCdnUrlsFromNetwork = videoCdnUrls.map((u) => u.slice(0, 140));

        // Include the final URL and diagnostics so the caller can detect redirects and failures.
        const diagInfo = { finalUrl, tabStatus, scriptError };
        if (tabDebug) Object.assign(tabDebug, diagInfo);
        sendResponse({ ok: true, urls: [...domUrls, ...videoCdnUrls], postContentUrls, reactionCount, linkAttachments, mainHtml, tabDebug: tabDebug || diagInfo, finalUrl });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      } finally {
        if (tabId != null) {
          delete _diagnosticWebRequestLogByTab[tabId];
          chrome.tabs.remove(tabId).catch(() => {});
        }
        // If the export window has no remaining tabs, close it proactively.
        // Use the idempotent helper so the persisted id is also cleared.
        if (_exportWindowId !== null) {
          chrome.tabs.query({ windowId: _exportWindowId }).then((tabs) => {
            // Only the initial about:blank tab (or none) remains
            if (!tabs || tabs.length <= 1) {
              closeExportWindowIfExists();
            }
          }).catch(() => {});
        }
      }
    })();
    return true;
  }

  // ── Close the dedicated export window when enrichment is done ──
  if (msg.type === 'FB_EXPORT_CLOSE_WINDOW') {
    closeExportWindowIfExists();
    sendResponse({ ok: true });
    return false;
  }

  // ── Stream a single file to disk via chrome.downloads (avoids accumulating
  //    bytes in the wizard heap). Called per-media-file by content.js.
  //    msg = { blobUrl: string, filename: string (relative to Downloads/) }
  if (msg.type === 'FB_EXPORT_SAVE_FILE') {
    (async () => {
      try {
        if (typeof msg.blobUrl !== 'string' || !msg.blobUrl) {
          sendResponse({ ok: false, error: 'missing blobUrl' });
          return;
        }
        if (typeof msg.filename !== 'string' || !msg.filename) {
          sendResponse({ ok: false, error: 'missing filename' });
          return;
        }
        const downloadId = await chrome.downloads.download({
          url: msg.blobUrl,
          filename: msg.filename,
          conflictAction: 'uniquify',
          saveAs: false,
        });
        const finalState = await new Promise((resolve) => {
          // 5 min per-file ceiling. Most media files are <1 MB and complete in
          // well under a second; large videos (up to ~30 MB) can take longer
          // on slow connections. The wait protects against the
          // chrome.downloads queue getting stuck silently — if the timeout
          // hits, we resolve as 'timeout' and the wizard records a media
          // error for that URL rather than hanging the whole export.
          const timeoutMs = 5 * 60 * 1000;
          const t = setTimeout(() => {
            chrome.downloads.onChanged.removeListener(listener);
            resolve('timeout');
          }, timeoutMs);
          const listener = (delta) => {
            if (delta.id !== downloadId) return;
            if (delta.state && delta.state.current !== 'in_progress') {
              clearTimeout(t);
              chrome.downloads.onChanged.removeListener(listener);
              resolve(delta.state.current);
            }
          };
          chrome.downloads.onChanged.addListener(listener);
        });
        sendResponse({ ok: finalState === 'complete', state: finalState, downloadId });
      } catch (err) {
        sendResponse({ ok: false, error: String(err) });
      }
    })();
    return true;
  }

  // ── Diagnostic webRequest capture control ──
  if (msg.type === 'FB_EXPORT_DIAG_START_WEBREQUEST') {
    const diagTabId = msg.tabId;
    if (diagTabId != null) _diagnosticWebRequestLogByTab[diagTabId] = [];
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'FB_EXPORT_DIAG_GET_WEBREQUEST') {
    const diagTabId = msg.tabId;
    const log = (diagTabId != null ? _diagnosticWebRequestLogByTab[diagTabId] : null) ?? [];
    if (diagTabId != null) delete _diagnosticWebRequestLogByTab[diagTabId];
    sendResponse({ ok: true, log });
    return false;
  }

  return false;
});
