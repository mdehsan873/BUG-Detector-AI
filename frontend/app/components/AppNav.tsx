"use client";

import { useAuth } from "@/lib/auth";
import { usePathname } from "next/navigation";

// Pages that handle their own nav (landing, login, signup)
const NO_NAV_ROUTES = ["/", "/login", "/signup"];

export function AppNav() {
  const { user, logout } = useAuth();
  const pathname = usePathname();

  // Don't render the app nav on public pages — they have their own navigation
  if (NO_NAV_ROUTES.includes(pathname)) {
    return null;
  }

  // Authenticated app nav
  return (
    <nav className="bg-white border-b border-gray-200 px-6 py-3.5">
      <div className="max-w-5xl mx-auto flex items-center justify-between">
        <div className="flex items-center gap-6">
          <a href="/dashboard" className="flex items-center gap-2">
            <img src="/logo.svg" alt="Buglyft" className="w-7 h-7 rounded-md" />
            <span className="text-sm font-bold text-gray-900">Buglyft</span>
          </a>

          <div className="hidden sm:flex items-center gap-1">
            <a
              href="/dashboard"
              className={`text-sm px-3 py-1.5 rounded-md transition ${
                pathname === "/dashboard"
                  ? "bg-gray-100 text-gray-900 font-medium"
                  : "text-gray-500 hover:text-gray-900 hover:bg-gray-50"
              }`}
            >
              Projects
            </a>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {user && (
            <>
              <span className="text-xs text-gray-500 hidden sm:inline">
                {user.email}
              </span>
              <button
                onClick={logout}
                className="text-sm text-gray-500 hover:text-gray-900 transition px-3 py-1.5"
              >
                Log out
              </button>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
