// Node-side tests for lib/strip_noise.js.
//
// Pinned cases come from the real Activity-Log harvest merged on 2026-05-23
// (~/Downloads/fb-activity-export-merged-2026-05-23/posts.json). The 5
// inputs below are the verbatim content_text of 2015-era pfbid posts where
// the Googlebot public-bot fallback failed and the harvester captured the
// raw AL-row innerText — including the "shared a <noun>." prefix and the
// "Public<HH:MM><AM|PM>View" trailer. Without this fix, those rows would
// import with corrupt content_text, corrupt al_* source_ids (the hash is
// computed from the polluted text), and corrupt embeddings.
//
// Plus 2 cases for the pure-chrome rows that already polluted prod
// (al_b6f3031c800e5c1b, al_907ea48ef9768511): the entire body is the AL
// section header. Caller drops the row when stripActivityNoise returns ''.

const test = require('node:test');
const assert = require('node:assert/strict');
const { stripActivityNoise } = require('../lib/strip_noise.js');

// NNBSP = narrow no-break space, U+202F. FB uses it between HH:MM and AM/PM.
const NNBSP = ' ';

test('strips "shared a link." prefix + "Public<HH:MM> PMView" trailer (беженцы)', () => {
  const polluted =
    'shared a link.Да, "катастрофа" с беженцами существует только в зомбоящике. Развитые страны так или иначе принимают тысячи беженцев постоянно десятилетиями. ' +
    'Если справились с русскими волнами 70-90х (а конкретно Германия умудрилась пережить полноценную советскую оккупацию на десятилетия), то сейчас и подавно не утратят свой "культурный код". Добавят несколько бит, и делов, это ж не совок, не обязательно выпиливать все отличающееся.\n' +
    'А ксенофобия, как правильно заметил Noize MC, удел неандертальцев по типу Кати Андреевой, Михаила Веллера и Евгения Гришковца. Они-то знают, как сохранять истинную европейскую культуру и защищать от чужеродцев. К счастью для Европы, они эту культуру лелеют на дальних подступах.' +
    `Public6:58${NNBSP}PMView`;
  const cleaned = stripActivityNoise(polluted);
  assert.ok(cleaned.startsWith('Да, "катастрофа"'), `prefix not stripped: ${cleaned.slice(0, 50)}`);
  assert.ok(cleaned.endsWith('на дальних подступах.'), `trailer not stripped: ${cleaned.slice(-50)}`);
  assert.match(cleaned, /Кати Андреевой/);
  assert.doesNotMatch(cleaned, /^shared a/i);
  assert.doesNotMatch(cleaned, /Public\d/i);
  assert.doesNotMatch(cleaned, /View$/);
});

test('strips "shared a ." (empty-noun) prefix + AM trailer (sonic/Comcast)', () => {
  const polluted =
    'shared a link.Не знаю уж про планы Соника сделать везде fiber, но что AT&T, что Comcast настолько ужасны (а это почти весь интернет в асашай), что я конечно же переключусь на любую альтернативу, тем более что они смогли сделать нормальный сайт.\n' +
    'https://www.sonic.com/availability и наш старый (текущий) адрес, и новый доступны, при переезде выкину Comcast с огромным удовольствием на свалку истории.' +
    `Public12:59${NNBSP}AMView`;
  const cleaned = stripActivityNoise(polluted);
  assert.ok(cleaned.startsWith('Не знаю уж про планы'), `prefix not stripped: ${cleaned.slice(0, 50)}`);
  assert.ok(cleaned.endsWith('свалку истории.'), `trailer not stripped: ${cleaned.slice(-50)}`);
  assert.doesNotMatch(cleaned, /^shared a/i);
  assert.doesNotMatch(cleaned, /Public\d/i);
});

test('strips empty-noun "shared a ." prefix and PM trailer (традиционные ценности)', () => {
  const polluted = `shared a .доходчиво о "традиционных ценностях"Public11:55${NNBSP}PMView`;
  const cleaned = stripActivityNoise(polluted);
  assert.equal(cleaned, 'доходчиво о "традиционных ценностях"');
});

test('strips "shared a post." prefix and PM trailer with tagged-friends-only body (no commentary)', () => {
  // Genuine tagged-friends share with no body commentary. The tagged
  // friends remain — they're real content, not chrome.
  const polluted =
    'shared a post.Nikita Popov Victor Denisov Виталий Гольдштейн Oleg Priadko Sergey Nazarov Andrey-Sergey Kim' +
    `Public7:26${NNBSP}PMView`;
  const cleaned = stripActivityNoise(polluted);
  assert.equal(
    cleaned,
    'Nikita Popov Victor Denisov Виталий Гольдштейн Oleg Priadko Sergey Nazarov Andrey-Sergey Kim',
  );
});

test('strips "shared a ." prefix when body has no trailer (long body, Физике)', () => {
  // The Googlebot fetch can return enough text that the trailer is dropped
  // upstream (truncated); only the prefix survives in row.innerText.
  const polluted =
    'shared a .Физике нас в школе учили хорошо и много, но теоретически. Собрать электромагнит, объяснить электрофорную машину, вылить за шиворот Паше Д. жидкого азота — вот и весь hands-on experience за четыре года.';
  const cleaned = stripActivityNoise(polluted);
  assert.ok(cleaned.startsWith('Физике'), `prefix not stripped: ${cleaned.slice(0, 30)}`);
  assert.doesNotMatch(cleaned, /^shared a/i);
});

test('strips leading "<Date>View" header before the action prefix (prod al_3f5d6c8466e1a237)', () => {
  // This shape made it into prod (5 rows). The AL row text starts with the
  // date stamp + "View" because the SPA's date header bled into the body.
  const polluted = 'February 20, 2021View shared a .акабы хуже говна.';
  const cleaned = stripActivityNoise(polluted);
  assert.equal(cleaned, 'акабы хуже говна.');
});

test('drops AL section header rows entirely (returns empty)', () => {
  // Pure chrome — entire body is the section header captured because the
  // row container heuristic walked too far up the DOM. Two prod examples:
  // al_b6f3031c800e5c1b, al_907ea48ef9768511.
  const headerOnly =
    'Your posts, photos and videosAllArchiveTrashChange AudienceDecember 18, 2023December 17, 2023December 16, 2023';
  assert.equal(stripActivityNoise(headerOnly), '');
});

test('does NOT corrupt a clean modern post (no prefix, no trailer)', () => {
  const clean = 'Nuff said';
  assert.equal(stripActivityNoise(clean), 'Nuff said');
});

test('does NOT corrupt a clean post that happens to mention "shared a link" mid-body', () => {
  const clean =
    'My friend shared a link to a really interesting article today; here is what they said about it.';
  assert.equal(stripActivityNoise(clean), clean);
});

test('does NOT corrupt a clean post that ends with the word "View" but not the trailer pattern', () => {
  const clean = 'I have a different point of View.';
  assert.equal(stripActivityNoise(clean), clean);
});

test('preserves multi-paragraph content with internal line breaks', () => {
  const polluted =
    'shared a link.First paragraph.\n\nSecond paragraph with content.' + `Public6:58${NNBSP}PMView`;
  const cleaned = stripActivityNoise(polluted);
  assert.match(cleaned, /^First paragraph\./);
  assert.match(cleaned, /Second paragraph with content\.$/);
  assert.doesNotMatch(cleaned, /Public/);
});

test('strips bare "Public" trailer with no time / no View suffix', () => {
  // FB Activity Log sometimes emits the audience pill alone — observed in
  // 9 rows of the 2026-05-28 historical re-harvest, where prod has the
  // clean body and incoming has "<body>Public". Source_id is computed
  // from the cleaned text; without this fix, those rows hash differently
  // from prod and produce ghost dedup entries.
  const polluted = 'shared a link.Some content.Public';
  assert.equal(stripActivityNoise(polluted), 'Some content.');
});

test('strips bare "Friends" trailer without time', () => {
  // JS strip_noise.js only handles `shared a <noun>.` prefixes — other
  // action prefixes like "updated his status." are removed Python-side in
  // extractors/harvest_post_identity.py._clean_text. This test pins the
  // JS trailer behavior only.
  const polluted = 'shared a post.Hello world.Friends';
  assert.equal(stripActivityNoise(polluted), 'Hello world.');
});

test('strips audience marker + View with no time in between', () => {
  const polluted = 'shared a post.Body text.PublicView';
  assert.equal(stripActivityNoise(polluted), 'Body text.');
});
