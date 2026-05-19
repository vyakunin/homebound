/**
 * Opens the wizard in the side panel when the toolbar icon is clicked so it
 * stays visible while the Facebook tab navigates or reloads.
 */
function registerSidePanelClickOpensPanel() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
}

chrome.runtime.onInstalled.addListener(registerSidePanelClickOpensPanel);
registerSidePanelClickOpensPanel();

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
