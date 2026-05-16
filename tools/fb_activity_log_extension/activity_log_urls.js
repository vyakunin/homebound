/**
 * Activity Log deep links (facebook.com/me/...).
 * Meta changes query params by locale and over time — if auto-navigation is wrong,
 * copy the URL from your address bar with the correct filter active and paste into
 * the wizard URL fields (saved in chrome.storage.local).
 */
var FB_ACTIVITY_LOG_URLS = {
  comments:
    'https://www.facebook.com/me/allactivity?privacy_source=activity_log&category_key=commentscluster',
  posts:
    'https://www.facebook.com/me/allactivity?activity_history=false&category_key=MANAGEPOSTSPHOTOSANDVIDEOS&manage_mode=false&should_load_landing_page=false',
};
