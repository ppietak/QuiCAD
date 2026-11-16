// Languages of the interface. English is the source: every text in the code is
// English, and a language is one file in web/i18n/ that maps those texts to its
// own. Nothing else knows about languages.
//
//   t('Restore')                          -> 'Przywróć'
//   t('Copied {name}', { name })          -> placeholders are filled after lookup
//   plural(3, 'model', 'models')          -> '3 modele' (the language picks the form)
//   translate(serverText)                 -> texts that arrive already filled in
//                                            (errors, history subjects): exact
//                                            match first, then the file's patterns

export const LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'pl', label: 'Polski' },
];

const STORE_KEY = 'lang';
const ENGLISH = { code: 'en', locale: 'en-GB', strings: {}, plurals: {}, patterns: [], pluralIndex: (n) => (n === 1 ? 0 : 1) };

let current = ENGLISH;

/** The language to start in: the one chosen before, else the browser's if we have it. */
export function preferredLanguage() {
  let stored = null;
  try { stored = localStorage.getItem(STORE_KEY); } catch { /* storage may be off */ }
  const wanted = stored || (navigator.language || 'en').slice(0, 2).toLowerCase();
  return LANGUAGES.some((item) => item.code === wanted) ? wanted : 'en';
}

/** Load a language and make it current. Resolves to its code; falls back to English. */
export async function setLanguage(code, { remember = false } = {}) {
  let next = ENGLISH;
  if (code !== 'en' && LANGUAGES.some((item) => item.code === code)) {
    try {
      next = { ...ENGLISH, ...(await import(`./i18n/${code}.js`)).default, code };
    } catch (err) {
      console.error(`language ${code} could not be loaded`, err);
    }
  }
  current = next;
  document.documentElement.lang = current.code;
  if (remember) {
    try { localStorage.setItem(STORE_KEY, current.code); } catch { /* storage may be off */ }
  }
  return current.code;
}

export const language = () => current.code;

/** The locale for dates and numbers (Intl), e.g. 'pl-PL'. */
export const locale = () => current.locale;

function fill(text, vars) {
  return vars ? text.replace(/\{(\w+)\}/g, (whole, key) => (key in vars ? String(vars[key]) : whole)) : text;
}

/** A text of the interface in the current language. */
export function t(text, vars) {
  return fill(current.strings[text] ?? text, vars);
}

/** "3 models": the count with the right form of the noun. */
export function plural(count, one, many) {
  const forms = current.plurals[one];
  const word = forms ? forms[Math.min(current.pluralIndex(count), forms.length - 1)] : (count === 1 ? one : many);
  return `${count} ${word}`;
}

/**
 * A text that reaches the page already put together in English (a server
 * error, the subject of a version): translated when the language knows it.
 */
export function translate(text) {
  if (typeof text !== 'string' || current === ENGLISH) return text;
  const exact = current.strings[text];
  if (exact != null) return exact;
  for (const [pattern, replacement] of current.patterns) {
    if (pattern.test(text)) return text.replace(pattern, replacement);
  }
  return text;
}
