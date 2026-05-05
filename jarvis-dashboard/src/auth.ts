// Token management for Jarvis multi-user server.
// Token + username are stored in localStorage so they persist across page refreshes.

export interface AuthState {
  token:    string;
  username: string;
}

const TOKEN_KEY    = 'jarvis_token';
const USERNAME_KEY = 'jarvis_username';

export function getAuth(): AuthState | null {
  const token    = localStorage.getItem(TOKEN_KEY);
  const username = localStorage.getItem(USERNAME_KEY);
  if (!token || !username) return null;
  return { token, username };
}

export function setAuth(token: string, username: string) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USERNAME_KEY, username);
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USERNAME_KEY);
}

export function authHeaders(): Record<string, string> {
  const auth = getAuth();
  return auth ? { Authorization: `Bearer ${auth.token}` } : {};
}

export async function authedFetch(url: string, options: RequestInit = {}): Promise<Response> {
  return fetch(url, {
    ...options,
    headers: {
      ...(options.headers as Record<string, string> | undefined),
      ...authHeaders(),
    },
  });
}
