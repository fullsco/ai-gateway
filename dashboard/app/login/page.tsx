"use client";

import { createClient } from "@/lib/supabase/browser";
import { FormEvent, useState } from "react";

export default function Login() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const { error } = await createClient().auth.signInWithPassword({
      email: String(form.get("email")),
      password: String(form.get("password")),
    });
    if (error) {
      setError(error.message);
      setBusy(false);
      return;
    }
    // A hard navigation after sign-in re-runs the server component auth check so the
    // control plane renders with an established session, rather than a client push.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/");
  }
  return <main className="login"><form onSubmit={submit}><div className="mark">AG</div><h1>Operator access</h1><p>Authenticate with the Supabase administrator account.</p><label>Email<input name="email" type="email" autoComplete="email" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label>{error && <div className="form-error">{error}</div>}<button disabled={busy}>{busy ? "Authenticating..." : "Enter control plane"}</button></form></main>;
}
