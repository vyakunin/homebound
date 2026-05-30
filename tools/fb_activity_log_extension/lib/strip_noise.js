// Activity-Log row text sanitiser.
//
// Inputs are the visible innerText of an AL row container (post or comment).
// FB renders chrome around the actual content — action-label prefixes like
// "shared a link.", audience-pill trailers like "Public6:58 PMView", interaction
// buttons (Like/Reply/Comment), bare relative timestamps ("5h", "1d"), and
// sometimes the entire AL section header itself ("Your posts, photos and
// videos AllArchiveTrash Change Audience…") when a row got mis-attributed
// to the top of the page. This function strips all of that so what remains
// is the post's real body.
//
// History:
//   v2.8.19 first stripped a single Public-trailer shape (`Public<HH:MM>`).
//   v2.8.33 broadened to cover the 2015-era utime-only pollution surfaced
//     during the 2026-05-23 full-archive merge: prefix bleed of "shared a
//     <noun>.", trailer "Public<HH:MM><space?>(AM|PM)?View", and pure-chrome
//     AL-header rows. See tests/strip_noise_test.js for pinned cases.

const ACTION_NOUN_GROUP = '(?:link|post|video|photo|memory|story|reel)';

// `shared a <noun>.` or `shared a .` (empty-noun shape FB emits when the
// reshared content type is undetected). Matches at string start AFTER a
// possible leading "<Month Day, Year>View" date-header bleed that v2.8.x
// sometimes captured (see existing prod row al_3f5d6c8466e1a237).
const ACTION_PREFIX = new RegExp(
  `^(?:\\w+\\s+\\d+,\\s*\\d{4}\\s*View\\s*)?shared\\s+a\\s+(?:${ACTION_NOUN_GROUP}\\s*)?\\.\\s*`,
  'i'
);

// `PublicHH:MM(<space>AM|PM)?View` — the audience pill + post-footer "View"
// link concatenated. ` ` is the narrow-no-break space FB uses between
// HH:MM and AM/PM in GraphQL-rendered text.
const PUBLIC_TRAILER = /\s*(?:Public|Friends|Custom|Only me|Close Friends)(?:\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)?\s*(?:View)?\s*$/i;

// Pure-chrome row: the AL section header captured as if it were a post body.
// Two known prod examples: al_b6f3031c800e5c1b, al_907ea48ef9768511.
const AL_HEADER_ONLY = /^Your posts, photos and videos.*AllArchiveTrash/i;

function stripActivityNoise(s) {
  if (!s) return '';

  let out = s
    .replace(/ /g, ' ')
    .split(/\n+/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
    .filter((l) => !/^(Like|Reply|Comment|Share|More|See more|Hide|Following|Message|Save|Send)$/i.test(l))
    .filter((l) => !/^\d+\s*(h|min|s|d|w|y|mo|yr)\b/i.test(l))
    .filter((l) => !/^·+$/.test(l))
    .join('\n')
    .replace(/\s{2,}/g, ' ')
    .trim();

  // Drop the row entirely when content is just the AL section header.
  if (AL_HEADER_ONLY.test(out)) return '';

  // Strip the leading action-label and the trailing audience+View pill,
  // in that order. Both narrow-no-break ( ) and regular spaces are
  // pre-normalised by the   replace above, so the trailer regex
  // catches the GraphQL-rendered shape "Public7:26 PMView".
  out = out.replace(ACTION_PREFIX, '');
  out = out.replace(PUBLIC_TRAILER, '');

  return out.trim();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { stripActivityNoise, ACTION_PREFIX, PUBLIC_TRAILER, AL_HEADER_ONLY };
}
if (typeof globalThis !== 'undefined') {
  globalThis.stripActivityNoise_lib = stripActivityNoise;
}
