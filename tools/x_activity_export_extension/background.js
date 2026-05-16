/**
 * Opens the wizard in the side panel when the toolbar icon is clicked.
 */
function registerSidePanelClickOpensPanel() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
}

chrome.runtime.onInstalled.addListener(registerSidePanelClickOpensPanel);
registerSidePanelClickOpensPanel();
