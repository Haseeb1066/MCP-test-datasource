/** Shared Tableau viewer identity for Connected App (JWT sub). */

const STORAGE_KEY = "mcp-tableau-username";
const SETTINGS_KEY = "tableauUsername";

let _username: string | null = null;
let _uniqueUserId: string | null = null;

export function getTableauUsername(): string | null {
  return _username;
}

export function getUniqueUserId(): string | null {
  return _uniqueUserId;
}

export function setUniqueUserId(id: string | null): void {
  _uniqueUserId = id?.trim() || null;
}

export function setTableauUsername(username: string | null): void {
  const cleaned = username?.trim() || null;
  _username = cleaned;
  try {
    if (cleaned) localStorage.setItem(STORAGE_KEY, cleaned);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function loadStoredTableauUsername(): string | null {
  if (_username) return _username;
  try {
    const fromLs = localStorage.getItem(STORAGE_KEY)?.trim();
    if (fromLs) {
      _username = fromLs;
      return fromLs;
    }
  } catch {
    /* ignore */
  }
  return null;
}

export function settingsUsernameKey(): string {
  return SETTINGS_KEY;
}

/** Append tableauUsername to API query strings when set. */
export function withTableauUser(qs: URLSearchParams): URLSearchParams {
  const u = getTableauUsername();
  if (u) qs.set("tableauUsername", u);
  return qs;
}
