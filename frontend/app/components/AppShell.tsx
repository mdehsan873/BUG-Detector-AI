"use client";

import { usePathname } from "next/navigation";
import { Sidebar } from "./Sidebar";

const PUBLIC_ROUTES = ["/", "/login", "/signup", "/auth/callback"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isPublic = PUBLIC_ROUTES.includes(pathname);

  if (isPublic) {
    return <>{children}</>;
  }

  return (
    <div className="flex min-h-screen bg-[#f8f9fb]">
      <Sidebar />
      <main className="flex-1 ml-[240px]">
        <div className="max-w-6xl mx-auto px-10 py-9">
          {children}
        </div>
      </main>
    </div>
  );
}
