import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, api } from "../api/client";
import type { Session } from "../api/types";
import { sessionFromToken } from "../lib/session";

interface LoginDialogProps {
  onSignedIn: (session: Session) => void;
  onClose: () => void;
}

/** Sign-in for analysts who review burned-area products. Reading needs no account. */
export function LoginDialog({ onSignedIn, onClose }: LoginDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const token = await api.login(email.trim(), password);
      onSignedIn(sessionFromToken(email.trim(), token, Date.now()));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setError("Incorrect e-mail or password.");
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose} aria-labelledby="login-title">
      <form onSubmit={(event) => void submit(event)}>
        <h2 id="login-title">Analyst sign-in</h2>
        <p className="muted">
          Only needed to review burned-area products. Accounts are created by an administrator
          with <code>narsat users create</code>.
        </p>
        <label>
          E-mail
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(change) => setEmail(change.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(change) => setPassword(change.target.value)}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="button-row">
          <button type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <button type="button" className="quiet" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </dialog>
  );
}
