"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { listProjects, updateProjectProvider } from "@/lib/api";
import { Project, ProviderUpdate } from "@/lib/types";
import Link from "next/link";

const PROVIDER_LABELS: Record<string, string> = {
  posthog: "PostHog",
  fullstory: "FullStory",
  logrocket: "LogRocket",
  clarity: "Microsoft Clarity",
};

const PROVIDER_COLORS: Record<string, string> = {
  posthog: "bg-blue-500",
  fullstory: "bg-purple-500",
  logrocket: "bg-violet-500",
  clarity: "bg-sky-500",
};

const integrations = [
  {
    id: "posthog",
    name: "PostHog",
    description: "Session replay & event analytics. Connects to fetch user sessions and detect anomalies.",
    icon: (
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <rect width="24" height="24" rx="8" fill="#1D4AFF" />
        <path d="M7 8h10M7 12h10M7 16h6" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    accent: "border-blue-200 bg-blue-50/40",
  },
  {
    id: "fullstory",
    name: "FullStory",
    description: "Digital experience intelligence. Captures sessions with full context for AI bug analysis.",
    icon: (
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <rect width="24" height="24" rx="8" fill="#6B2CF5" />
        <path d="M8 8h8M8 12h6M8 16h4" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    accent: "border-purple-200 bg-purple-50/40",
    comingSoon: true,
  },
  {
    id: "logrocket",
    name: "LogRocket",
    description: "Session replay & performance monitoring. Captures user interactions and errors for analysis.",
    icon: (
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <rect width="24" height="24" rx="8" fill="#764ABC" />
        <circle cx="12" cy="12" r="4" stroke="white" strokeWidth="1.5" fill="none" />
        <path d="M12 4v2M12 18v2M4 12h2M18 12h2" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    accent: "border-violet-200 bg-violet-50/40",
    comingSoon: true,
  },
  {
    id: "clarity",
    name: "Microsoft Clarity",
    description: "Free session recordings & heatmaps. Provides user behavior insights for bug detection.",
    icon: (
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <rect width="24" height="24" rx="8" fill="#0078D4" />
        <path d="M8 8l8 4-8 4V8z" fill="white" />
      </svg>
    ),
    accent: "border-sky-200 bg-sky-50/40",
    comingSoon: true,
  },
  {
    id: "github",
    name: "GitHub",
    description: "Issue tracker. Automatically creates and updates issues when bugs are detected.",
    icon: (
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
        <rect width="24" height="24" rx="8" fill="#1b1f23" />
        <path d="M12 6C8.686 6 6 8.686 6 12c0 2.65 1.718 4.9 4.104 5.693.3.055.41-.13.41-.29 0-.142-.006-.52-.009-1.02-1.669.362-2.02-.805-2.02-.805-.273-.693-.666-.878-.666-.878-.545-.372.041-.365.041-.365.602.042.919.618.919.618.535.917 1.403.652 1.745.499.054-.388.21-.652.38-.802-1.332-.152-2.733-.666-2.733-2.965 0-.655.234-1.19.618-1.61-.062-.151-.268-.762.058-1.588 0 0 .504-.161 1.65.615a5.74 5.74 0 011.502-.202c.51.002 1.023.069 1.502.202 1.145-.776 1.648-.615 1.648-.615.328.826.122 1.437.06 1.588.385.42.617.955.617 1.61 0 2.305-1.404 2.812-2.74 2.96.215.186.407.553.407 1.114 0 .804-.007 1.453-.007 1.65 0 .161.108.348.413.29C16.284 16.897 18 14.648 18 12c0-3.314-2.686-6-6-6z" fill="white" />
      </svg>
    ),
    accent: "border-slate-200 bg-slate-50/40",
  },
];

/* ── Inline Config Panel ───────────────────────────────────────────────── */

function IntegrationConfigPanel({
  integrationId,
  projects,
  onClose,
  onSaved,
}: {
  integrationId: string;
  projects: Project[];
  onClose: () => void;
  onSaved: () => void;
}) {
  // Find projects using this provider (or all projects for github)
  const relevantProjects =
    integrationId === "github"
      ? projects
      : projects.filter((p) => (p.session_provider || "posthog") === integrationId);

  const [selectedProjectId, setSelectedProjectId] = useState(
    relevantProjects[0]?.id || projects[0]?.id || ""
  );
  const selectedProject = projects.find((p) => p.id === selectedProjectId);

  // Form state
  const [providerHost, setProviderHost] = useState(selectedProject?.provider_host || "");
  const [providerProjectId, setProviderProjectId] = useState(selectedProject?.provider_project_id || "");
  const [apiKey, setApiKey] = useState("");
  const [githubRepo, setGithubRepo] = useState(selectedProject?.github_repo || "");
  const [githubToken, setGithubToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState("");

  // Update form when selected project changes
  useEffect(() => {
    if (!selectedProject) return;
    setProviderHost(selectedProject.provider_host || "");
    setProviderProjectId(selectedProject.provider_project_id || "");
    setGithubRepo(selectedProject.github_repo || "");
    setApiKey("");
    setGithubToken("");
    setSuccess(false);
    setError("");
  }, [selectedProjectId, selectedProject]);

  const handleSave = async () => {
    setSaving(true);
    setError("");
    setSuccess(false);
    try {
      if (integrationId === "github") {
        // For GitHub we'd need a separate endpoint — for now update provider with same values
        // In a real app, you'd have a dedicated github config endpoint
        await updateProjectProvider(selectedProjectId, {
          session_provider: selectedProject?.session_provider || "posthog",
          provider_api_key: "", // don't change
          provider_project_id: selectedProject?.provider_project_id || "",
          provider_host: selectedProject?.provider_host || "",
        });
      } else {
        const update: ProviderUpdate = {
          session_provider: integrationId,
          provider_api_key: apiKey,
          provider_project_id: providerProjectId,
          provider_host: providerHost,
        };
        await updateProjectProvider(selectedProjectId, update);
      }
      setSuccess(true);
      onSaved();
    } catch (e: any) {
      setError(e.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const providerLabel = PROVIDER_LABELS[integrationId] || integrationId;
  const isGithub = integrationId === "github";

  return (
    <div className="mt-4 bg-white border border-indigo-200 rounded-2xl p-6 shadow-md animate-in slide-in-from-top-2">
      <div className="flex items-center justify-between mb-5">
        <h3 className="text-[15px] font-semibold text-slate-900">
          Configure {providerLabel}
        </h3>
        <button
          onClick={onClose}
          className="w-7 h-7 rounded-lg hover:bg-slate-100 flex items-center justify-center transition"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 6L6 18" />
            <path d="M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* Project selector */}
      <div className="mb-5">
        <label className="block text-xs font-semibold text-slate-600 mb-1.5">Project</label>
        <select
          value={selectedProjectId}
          onChange={(e) => setSelectedProjectId(e.target.value)}
          className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      {isGithub ? (
        /* GitHub config */
        <div className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">Repository</label>
            <input
              type="text"
              value={githubRepo}
              onChange={(e) => setGithubRepo(e.target.value)}
              placeholder="owner/repo"
              className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
            />
            <p className="text-[11px] text-slate-400 mt-1">e.g. acme-inc/frontend</p>
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">Personal Access Token</label>
            <input
              type="password"
              value={githubToken}
              onChange={(e) => setGithubToken(e.target.value)}
              placeholder="ghp_xxxxxxxxxxxx"
              className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
            />
            <p className="text-[11px] text-slate-400 mt-1">Needs repo scope for issue creation</p>
          </div>
        </div>
      ) : (
        /* Session provider config */
        <div className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">Host / Instance URL</label>
            <input
              type="text"
              value={providerHost}
              onChange={(e) => setProviderHost(e.target.value)}
              placeholder={integrationId === "posthog" ? "eu.posthog.com" : `app.${integrationId}.com`}
              className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">Project ID</label>
            <input
              type="text"
              value={providerProjectId}
              onChange={(e) => setProviderProjectId(e.target.value)}
              placeholder="12345"
              className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1.5">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="phx_xxxxxxxxxxxxxxxx"
              className="w-full px-3 py-2 border border-slate-200 rounded-xl text-sm text-slate-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-300 transition"
            />
            <p className="text-[11px] text-slate-400 mt-1">Leave blank to keep existing key</p>
          </div>
        </div>
      )}

      {/* Status messages */}
      {error && (
        <div className="mt-4 text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          {error}
        </div>
      )}
      {success && (
        <div className="mt-4 text-xs text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-lg px-3 py-2">
          Configuration saved successfully.
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-3 mt-5 pt-4 border-t border-slate-100">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-5 py-2 bg-indigo-600 text-white text-sm font-semibold rounded-xl hover:bg-indigo-700 transition disabled:opacity-50 shadow-sm shadow-indigo-500/15"
        >
          {saving ? "Saving..." : "Save Configuration"}
        </button>
        <button
          onClick={onClose}
          className="px-4 py-2 text-sm font-medium text-slate-600 hover:text-slate-800 transition"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/* ── Main Page ──────────────────────────────────────────────────────────── */

export default function IntegrationsPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [configOpen, setConfigOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!authLoading && !user) router.push("/login");
  }, [user, authLoading, router]);

  const fetchProjects = () => {
    if (!user) return;
    listProjects()
      .then(setProjects)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  if (authLoading || !user) {
    return (
      <div className="flex items-center justify-center py-20">
        <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      </div>
    );
  }

  // Count projects per provider
  const providerCounts: Record<string, number> = {};
  for (const p of projects) {
    const prov = p.session_provider || "posthog";
    providerCounts[prov] = (providerCounts[prov] || 0) + 1;
  }
  const githubCount = projects.filter((p) => p.github_repo).length;

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-[22px] font-bold text-slate-900">Integrations</h1>
        <p className="text-sm text-slate-500 mt-1">
          Manage your connected services. Each project connects to a session replay provider and GitHub.
        </p>
      </div>

      {/* Integration cards */}
      <div className="grid gap-4">
        {integrations.map((integration) => {
          const isComingSoon = "comingSoon" in integration && integration.comingSoon;
          const count = integration.id === "github"
            ? githubCount
            : (providerCounts[integration.id] || 0);
          const isConfigOpen = configOpen === integration.id;

          return (
            <div key={integration.id}>
              <div
                className={`bg-white border rounded-2xl p-6 flex items-start gap-5 shadow-sm transition-shadow ${isComingSoon ? "opacity-60" : "hover:shadow-md"} ${integration.accent}`}
              >
                <div className="shrink-0 mt-0.5">{integration.icon}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2.5 mb-1.5">
                    <h3 className="font-semibold text-slate-900 text-[15px]">{integration.name}</h3>
                    {isComingSoon ? (
                      <span className="text-[11px] font-semibold bg-violet-50 text-violet-600 border border-violet-100 px-2.5 py-0.5 rounded-full">
                        Coming Soon
                      </span>
                    ) : count > 0 ? (
                      <span className="text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-100 px-2.5 py-0.5 rounded-full">
                        {count} project{count > 1 ? "s" : ""} connected
                      </span>
                    ) : (
                      <span className="text-[11px] font-semibold bg-slate-50 text-slate-500 border border-slate-100 px-2.5 py-0.5 rounded-full">
                        Not connected
                      </span>
                    )}
                  </div>
                  <p className="text-sm text-slate-500 leading-relaxed">{integration.description}</p>
                </div>
                <div className="shrink-0 flex flex-col gap-2 items-end">
                  {!isComingSoon && (
                    <>
                      {projects.length > 0 ? (
                        <button
                          onClick={() => setConfigOpen(isConfigOpen ? null : integration.id)}
                          className={`text-sm font-semibold px-4 py-2 border rounded-xl transition ${
                            isConfigOpen
                              ? "text-indigo-700 border-indigo-300 bg-indigo-100"
                              : "text-indigo-600 border-indigo-200 bg-indigo-50/50 hover:bg-indigo-50 hover:text-indigo-700"
                          }`}
                        >
                          {isConfigOpen ? "Close" : "Configure"}
                        </button>
                      ) : (
                        <Link
                          href="/projects/new"
                          className="text-sm font-semibold text-indigo-600 hover:text-indigo-700 px-4 py-2 border border-indigo-200 bg-indigo-50/50 rounded-xl hover:bg-indigo-50 transition"
                        >
                          Add Project First
                        </Link>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* Inline config panel */}
              {isConfigOpen && projects.length > 0 && (
                <IntegrationConfigPanel
                  integrationId={integration.id}
                  projects={projects}
                  onClose={() => setConfigOpen(null)}
                  onSaved={() => fetchProjects()}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* Connected projects */}
      {projects.length > 0 && (
        <div className="mt-10">
          <h2 className="text-lg font-semibold text-slate-900 mb-4">Connected Projects</h2>
          <div className="grid gap-3">
            {projects.map((project) => {
              const prov = project.session_provider || "posthog";
              return (
                <div
                  key={project.id}
                  className="bg-white border border-slate-200/80 rounded-2xl p-5 flex items-center justify-between hover:border-indigo-200 hover:shadow-md transition-all group"
                >
                  <Link href={`/projects/${project.id}`} className="flex-1 min-w-0">
                    <h3 className="font-semibold text-slate-900 group-hover:text-indigo-700 transition-colors">{project.name}</h3>
                    <div className="flex items-center gap-4 mt-2">
                      <span className="text-xs text-slate-500 flex items-center gap-1.5 font-medium">
                        <span className={`w-2 h-2 rounded-full ${PROVIDER_COLORS[prov] || "bg-blue-500"} inline-block`} />
                        {PROVIDER_LABELS[prov] || "PostHog"}
                      </span>
                      {project.github_repo && (
                        <span className="text-xs text-slate-500 flex items-center gap-1.5 font-medium">
                          <span className="w-2 h-2 rounded-full bg-slate-700 inline-block" />
                          {project.github_repo}
                        </span>
                      )}
                    </div>
                  </Link>
                  <Link
                    href={`/projects/${project.id}`}
                    className="shrink-0 text-xs font-semibold text-indigo-600 hover:text-indigo-700 px-3 py-1.5 border border-indigo-200 bg-indigo-50/50 rounded-lg hover:bg-indigo-50 transition ml-3"
                  >
                    View Project
                  </Link>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
