// Do not hard-code localhost: it points at the administrator's own computer
// when this panel is opened from another device.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || `${window.location.protocol}//${window.location.hostname}:8002/api`;
export const TOKEN_KEY = 'adminToken';

export async function getApiError(response) {
  const body = await response.json().catch(() => null);
  return body?.detail || body?.message || `Request failed (${response.status})`;
}

// fetch() that sends the admin token; a 401 (expired or missing login) sends the user back to the login screen.
export function authFetch(url, opts = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  const headers = { ...(opts.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  return fetch(url, { ...opts, headers }).then((res) => {
    if (res.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.location.reload();
    }
    return res;
  });
}
