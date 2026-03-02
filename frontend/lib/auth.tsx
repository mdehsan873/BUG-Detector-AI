"use client";

import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface User {
  id: string;
  email: string;
  name: string;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (name: string, email: string, password: string) => Promise<{ confirmation_pending?: boolean }>;
  logout: () => void;
  refreshAccessToken: () => Promise<string | null>;
  /** Store tokens from Supabase email verification redirect and fetch user profile. */
  handleAuthCallback: (accessToken: string, refreshToken: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// ── Storage helpers ─────────────────────────────────────────────────────────

function getToken(key: string): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage?.getItem(key) || null;
}

function setToken(key: string, value: string) {
  if (typeof window !== "undefined") window.sessionStorage?.setItem(key, value);
}

function removeToken(key: string) {
  if (typeof window !== "undefined") window.sessionStorage?.removeItem(key);
}

// ── Provider ────────────────────────────────────────────────────────────────

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Attempt to refresh the access token using the stored refresh token.
  // Returns the new access token on success, or null on failure.
  const refreshAccessToken = useCallback(async (): Promise<string | null> => {
    const refreshToken = getToken("refresh_token");
    if (!refreshToken) return null;

    try {
      const res = await fetch(`${API_URL}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });

      if (!res.ok) {
        // Refresh token is also expired — full logout
        removeToken("auth_token");
        removeToken("refresh_token");
        setUser(null);
        return null;
      }

      const data = await res.json();
      setToken("auth_token", data.access_token);
      if (data.refresh_token) setToken("refresh_token", data.refresh_token);
      setUser(data.user);
      return data.access_token;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    // On mount: try existing access token, fall back to refresh
    const token = getToken("auth_token");
    if (token) {
      fetchMe(token)
        .then(setUser)
        .catch(async () => {
          // Access token expired — try refresh
          const newToken = await refreshAccessToken();
          if (!newToken) {
            removeToken("auth_token");
            removeToken("refresh_token");
          }
        })
        .finally(() => setLoading(false));
    } else {
      // No access token — attempt silent refresh
      refreshAccessToken().finally(() => setLoading(false));
    }
  }, [refreshAccessToken]);

  const login = async (email: string, password: string) => {
    const res = await fetch(`${API_URL}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Login failed" }));
      throw new Error(err.detail || "Login failed");
    }
    const data = await res.json();
    setToken("auth_token", data.access_token);
    if (data.refresh_token) setToken("refresh_token", data.refresh_token);
    setUser(data.user);
  };

  const signup = async (name: string, email: string, password: string): Promise<{ confirmation_pending?: boolean }> => {
    const res = await fetch(`${API_URL}/auth/signup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, email, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Signup failed" }));
      throw new Error(err.detail || "Signup failed");
    }
    const data = await res.json();

    // Email confirmation required — don't store tokens or set user yet
    if (data.confirmation_pending) {
      return { confirmation_pending: true };
    }

    setToken("auth_token", data.access_token);
    if (data.refresh_token) setToken("refresh_token", data.refresh_token);
    setUser(data.user);
    return {};
  };

  const handleAuthCallback = useCallback(async (accessToken: string, refreshToken: string) => {
    setToken("auth_token", accessToken);
    if (refreshToken) setToken("refresh_token", refreshToken);
    const me = await fetchMe(accessToken);
    setUser(me);
  }, []);

  const logout = () => {
    removeToken("auth_token");
    removeToken("refresh_token");
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, logout, refreshAccessToken, handleAuthCallback }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

async function fetchMe(token: string): Promise<User> {
  const res = await fetch(`${API_URL}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("Session expired");
  return res.json();
}
