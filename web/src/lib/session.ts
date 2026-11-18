/** The signed-in analyst. Kept for the browser tab only (sessionStorage), never on disk. */
import type { Session, TokenResponse } from "../api/types";

const KEY = "narsat.session";

export function sessionFromToken(email: string, token: TokenResponse, now: number): Session {
  return {
    token: token.access_token,
    email,
    role: token.role,
    // Stop using the token a little before the server would refuse it.
    expiresAt: now + Math.max(0, token.expires_in - 30) * 1000,
  };
}

export function isLive(session: Session | null, now: number): session is Session {
  return session !== null && session.expiresAt > now;
}

export function loadSession(now: number): Session | null {
  try {
    const raw = globalThis.sessionStorage?.getItem(KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as Session;
    return isLive(session, now) ? session : null;
  } catch {
    return null; // storage unavailable or content unreadable: start signed out
  }
}

export function storeSession(session: Session | null): void {
  try {
    if (session) globalThis.sessionStorage?.setItem(KEY, JSON.stringify(session));
    else globalThis.sessionStorage?.removeItem(KEY);
  } catch {
    // Private browsing or blocked storage: the session simply lasts until reload.
  }
}
