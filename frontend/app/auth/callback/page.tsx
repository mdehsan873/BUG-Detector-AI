"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

/**
 * Handles the redirect from Supabase email verification.
 *
 * Supabase redirects to:
 *   http://localhost:3000/auth/callback#access_token=...&refresh_token=...&type=signup
 *
 * We extract the tokens from the URL hash fragment, store them, and redirect
 * the user to the dashboard.
 */
export default function AuthCallbackPage() {
  const router = useRouter();
  const { handleAuthCallback } = useAuth();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function processCallback() {
      try {
        // Parse the hash fragment — Supabase puts tokens there
        const hash = window.location.hash.substring(1); // remove the leading #
        const params = new URLSearchParams(hash);

        const accessToken = params.get("access_token");
        const refreshToken = params.get("refresh_token") || "";

        if (!accessToken) {
          setError("No access token found in the verification link. Please try logging in.");
          return;
        }

        // Store tokens and fetch user profile
        await handleAuthCallback(accessToken, refreshToken);

        // Redirect to dashboard
        router.replace("/dashboard");
      } catch (err) {
        console.error("Auth callback error:", err);
        setError("Something went wrong verifying your email. Please try logging in.");
      }
    }

    processCallback();
  }, [handleAuthCallback, router]);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f8f9fb] px-6">
        <div className="w-full max-w-md text-center">
          <div className="mx-auto w-16 h-16 bg-red-100 rounded-full flex items-center justify-center mb-6">
            <svg className="w-8 h-8 text-red-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="15" y1="9" x2="9" y2="15" />
              <line x1="9" y1="9" x2="15" y2="15" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-slate-900 mb-2">Verification failed</h1>
          <p className="text-sm text-slate-500 mb-6">{error}</p>
          <a
            href="/login"
            className="inline-block bg-indigo-600 text-white px-6 py-2.5 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition"
          >
            Go to login
          </a>
        </div>
      </div>
    );
  }

  // Loading state while processing tokens
  return (
    <div className="min-h-screen flex items-center justify-center bg-[#f8f9fb]">
      <div className="text-center">
        <svg className="animate-spin h-8 w-8 text-indigo-600 mx-auto mb-4" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        <p className="text-sm text-slate-500">Verifying your email...</p>
      </div>
    </div>
  );
}
