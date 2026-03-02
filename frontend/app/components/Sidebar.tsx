"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { listProjects } from "@/lib/api";
import { Project } from "@/lib/types";

const navItems = [
  {
    label: "Dashboard",
    href: "/dashboard",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
        <polyline points="9 22 9 12 15 12 15 22" />
      </svg>
    ),
  },
  {
    label: "Issues",
    href: "/issues",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
    ),
  },
  {
    label: "Integrations",
    href: "/integrations",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="2" width="8" height="8" rx="1" />
        <rect x="14" y="2" width="8" height="8" rx="1" />
        <rect x="2" y="14" width="8" height="8" rx="1" />
        <rect x="14" y="14" width="8" height="8" rx="1" />
      </svg>
    ),
  },
  {
    label: "Notifications",
    href: "/notifications",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
    ),
  },
];

export function Sidebar() {
  const { user, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>("");
  const [dropdownOpen, setDropdownOpen] = useState(false);

  useEffect(() => {
    if (!user) return;
    listProjects().then((projs) => {
      setProjects(projs);
      // Restore cached selection or default to newest
      const cached = typeof window !== "undefined" ? localStorage.getItem("selectedProjectId") : null;
      if (cached && projs.some((p) => p.id === cached)) {
        setSelectedProjectId(cached);
      } else if (projs.length > 0) {
        setSelectedProjectId(projs[0].id);
        if (typeof window !== "undefined") localStorage.setItem("selectedProjectId", projs[0].id);
      }
    }).catch(() => {});
  }, [user]);

  // Sync project ID from URL when navigating to a project page
  useEffect(() => {
    const match = pathname.match(/\/projects\/([^/]+)/);
    if (match && match[1] !== "new") {
      const urlProjectId = match[1];
      if (urlProjectId !== selectedProjectId && projects.some((p) => p.id === urlProjectId)) {
        setSelectedProjectId(urlProjectId);
        if (typeof window !== "undefined") localStorage.setItem("selectedProjectId", urlProjectId);
      }
    }
  }, [pathname, projects, selectedProjectId]);

  const handleProjectSelect = (projectId: string) => {
    setSelectedProjectId(projectId);
    if (typeof window !== "undefined") localStorage.setItem("selectedProjectId", projectId);
    setDropdownOpen(false);
    router.push(`/projects/${projectId}`);
  };

  const selectedProject = projects.find((p) => p.id === selectedProjectId);

  return (
    <aside className="fixed left-0 top-0 bottom-0 w-[240px] bg-[#0f1117] text-white flex flex-col z-40 border-r border-white/[0.06]">
      {/* Logo */}
      <div className="px-5 pt-7 pb-4">
        <Link href="/dashboard" className="flex items-center gap-3">
          <img src="/logo.svg" alt="Buglyft" className="w-9 h-9 rounded-xl shadow-lg shadow-indigo-500/20" />
          <div>
            <span className="text-[14px] font-semibold tracking-tight text-white">Buglyft</span>
            <span className="block text-[10px] font-medium text-indigo-400 tracking-wide uppercase">Pro Dashboard</span>
          </div>
        </Link>
      </div>

      {/* Project Dropdown */}
      {projects.length > 0 && (
        <div className="px-3 mb-4 relative">
          <button
            onClick={() => setDropdownOpen(!dropdownOpen)}
            className="w-full flex items-center justify-between gap-2 px-3 py-2.5 rounded-xl bg-white/[0.06] hover:bg-white/[0.1] border border-white/[0.08] transition text-left"
          >
            <div className="min-w-0">
              <p className="text-[11px] text-slate-500 font-medium uppercase tracking-wider">Project</p>
              <p className="text-[13px] font-semibold text-slate-200 truncate">
                {selectedProject?.name || "Select project"}
              </p>
            </div>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 transition-transform ${dropdownOpen ? "rotate-180" : ""}`}>
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>

          {dropdownOpen && (
            <div className="absolute left-3 right-3 top-full mt-1 bg-[#1a1d27] border border-white/[0.1] rounded-xl shadow-2xl py-1 z-50 max-h-64 overflow-y-auto">
              {projects.map((p) => (
                <button
                  key={p.id}
                  onClick={() => handleProjectSelect(p.id)}
                  className={`w-full text-left px-3 py-2.5 text-[13px] transition-colors ${
                    p.id === selectedProjectId
                      ? "bg-indigo-600/20 text-indigo-300 font-semibold"
                      : "text-slate-300 hover:bg-white/[0.06] font-medium"
                  }`}
                >
                  <span className="block truncate">{p.name}</span>
                  <span className="block text-[10px] text-slate-500 truncate mt-0.5">{p.github_repo}</span>
                </button>
              ))}
              <div className="border-t border-white/[0.08] mt-1 pt-1">
                <button
                  onClick={() => { setDropdownOpen(false); router.push("/projects/new"); }}
                  className="w-full text-left px-3 py-2.5 text-[13px] text-indigo-400 hover:bg-white/[0.06] font-semibold flex items-center gap-2"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                  </svg>
                  Create New Project
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Section label */}
      <div className="px-5 mb-2">
        <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Menu</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 space-y-1">
        {navItems.map((item) => {
          const isActive =
            pathname === item.href ||
            (item.href === "/dashboard" && pathname.startsWith("/projects"));
          // Notifications and Integrations need a selected project
          const needsProject = item.href === "/notifications" || item.href === "/integrations";
          const resolvedHref = item.href === "/dashboard" && selectedProjectId
            ? `/projects/${selectedProjectId}`
            : needsProject && selectedProjectId
              ? `${item.href}?project=${selectedProjectId}`
              : item.href;
          return (
            <Link
              key={item.href}
              href={resolvedHref}
              className={`group flex items-center gap-3 px-3 py-2.5 rounded-xl text-[13px] font-medium transition-all duration-150 ${
                isActive
                  ? "bg-indigo-600/15 text-indigo-400 shadow-sm"
                  : "text-slate-400 hover:text-slate-200 hover:bg-white/[0.04]"
              }`}
            >
              <span className={`transition-colors ${isActive ? "text-indigo-400" : "text-slate-500 group-hover:text-slate-300"}`}>
                {item.icon}
              </span>
              {item.label}
              {isActive && (
                <span className="ml-auto w-1.5 h-1.5 rounded-full bg-indigo-400" />
              )}
            </Link>
          );
        })}
      </nav>

      {/* User section */}
      {user && (
        <div className="px-3 pb-5 mt-auto">
          <div className="border-t border-white/[0.06] pt-5">
            <div className="flex items-center gap-3 px-3 mb-4">
              <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-xs font-bold text-white shrink-0 shadow-lg shadow-indigo-500/20">
                {user.name?.charAt(0)?.toUpperCase() || user.email?.charAt(0)?.toUpperCase()}
              </div>
              <div className="min-w-0">
                <p className="text-[13px] font-semibold text-slate-200 truncate">{user.name}</p>
                <p className="text-[11px] text-slate-500 truncate">{user.email}</p>
              </div>
            </div>
            <button
              onClick={logout}
              className="flex items-center gap-3 w-full px-3 py-2.5 text-[13px] font-medium text-slate-500 hover:text-red-400 hover:bg-red-500/[0.06] rounded-xl transition-all duration-150"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" />
                <polyline points="16 17 21 12 16 7" />
                <line x1="21" y1="12" x2="9" y2="12" />
              </svg>
              Log out
            </button>
          </div>
        </div>
      )}
    </aside>
  );
}
