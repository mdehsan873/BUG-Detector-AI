"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { createProject, validatePosthog, validateGithub } from "@/lib/api";

const PROVIDERS = [
  {
    id: "posthog",
    name: "PostHog",
    color: "#1D4AFF",
    bg: "bg-blue-50/50",
    border: "border-blue-100",
    apiKeyPlaceholder: "phx_...",
    projectIdLabel: "Project ID",
    projectIdPlaceholder: "12345",
    hasHostSelector: true,
    hosts: [
      { value: "eu.posthog.com", label: "EU Cloud (eu.posthog.com)" },
      { value: "us.posthog.com", label: "US Cloud (us.posthog.com)" },
      { value: "app.posthog.com", label: "US Legacy (app.posthog.com)" },
    ],
  },
  {
    id: "fullstory",
    name: "FullStory",
    color: "#6B2CF5",
    bg: "bg-purple-50/50",
    border: "border-purple-100",
    comingSoon: true,
  },
  {
    id: "logrocket",
    name: "LogRocket",
    color: "#764ABC",
    bg: "bg-violet-50/50",
    border: "border-violet-100",
    comingSoon: true,
  },
  {
    id: "clarity",
    name: "Microsoft Clarity",
    color: "#0078D4",
    bg: "bg-sky-50/50",
    border: "border-sky-100",
    comingSoon: true,
  },
];

// ── Collapsible setup instructions component ────────────────────────────────

function SetupInstructions({
  type,
}: {
  type: "posthog" | "github";
}) {
  const [open, setOpen] = useState(false);

  const posthogSteps = [
    { text: "Go to", highlight: "Settings \u2192 Personal API keys" },
    { text: "Click", highlight: "Create personal API key" },
    { text: 'Under Label, give your key a name like "Buglyft API Key"' },
    { text: "Under Organization & project access, click", highlight: "Projects", after: ", and select your project" },
    {
      text: "Under Scopes, enable these scopes with READ access:",
      list: ["Session Recording", "Person", "Query", "Error Tracking"],
    },
    { text: "Click", highlight: "Create Key", after: " and copy it" },
  ];

  const githubSteps = [
    { text: "Go to", highlight: "Settings \u2192 Developer settings \u2192 Personal access tokens \u2192 Fine-grained tokens" },
    { text: "Click", highlight: "Generate new token" },
    { text: "Give it a name and set an expiration (90 days recommended)" },
    { text: "Under Repository access, select", highlight: "Only select repositories", after: " and pick your repo" },
    {
      text: "Under Permissions \u2192 Repository permissions, enable:",
      list: ["Issues: Read and write", "Metadata: Read-only"],
    },
    { text: "Click", highlight: "Generate token", after: " and copy it" },
  ];

  const steps = type === "posthog" ? posthogSteps : githubSteps;
  const title = type === "posthog" ? "CREATE API KEY IN POSTHOG" : "CREATE PERSONAL ACCESS TOKEN";

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-[11px] text-indigo-600 font-semibold hover:text-indigo-700 flex items-center gap-1 transition"
      >
        {open ? "Hide" : "Show"} setup instructions
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`transition-transform ${open ? "rotate-180" : ""}`}
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open && (
        <div className="mt-3 bg-white border border-slate-200 rounded-xl p-4 space-y-0">
          <p className="text-[10px] font-semibold text-slate-400 tracking-wider mb-3">{title}</p>
          <ol className="space-y-2.5">
            {steps.map((step, idx) => (
              <li key={idx} className="flex items-start gap-3">
                <span className="flex-shrink-0 w-5 h-5 rounded-full bg-slate-100 text-slate-600 text-[10px] font-bold flex items-center justify-center mt-0.5">
                  {idx + 1}
                </span>
                <div className="text-xs text-slate-600 leading-relaxed">
                  {step.text}
                  {step.highlight && (
                    <>
                      {" "}
                      <code className="bg-slate-100 text-slate-800 px-1.5 py-0.5 rounded text-[11px] font-medium">
                        {step.highlight}
                      </code>
                    </>
                  )}
                  {step.after && <span>{step.after}</span>}
                  {step.list && (
                    <div className="mt-1.5 space-y-1 ml-1">
                      {step.list.map((item, i) => (
                        <div key={i} className="flex items-center gap-2">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M20 6L9 17l-5-5" />
                          </svg>
                          <span className="text-slate-700">{item}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

// ── Validation status badge ─────────────────────────────────────────────────

function ValidationBadge({
  status,
  message,
}: {
  status: "idle" | "loading" | "success" | "error";
  message?: string;
}) {
  if (status === "idle") return null;

  if (status === "loading") {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-slate-500 font-medium">
        <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        Testing...
      </div>
    );
  }

  if (status === "success") {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-emerald-600 font-semibold">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M20 6L9 17l-5-5" />
        </svg>
        {message || "Connected!"}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5 text-[11px] text-red-600 font-medium">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
      {message || "Connection failed"}
    </div>
  );
}

// ── Main Page ───────────────────────────────────────────────────────────────

export default function NewProjectPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const initialProvider = searchParams.get("provider") || "posthog";
  const validProvider = PROVIDERS.find((p) => p.id === initialProvider) ? initialProvider : "posthog";
  const initialHost = PROVIDERS.find((p) => p.id === validProvider)?.hosts?.[0]?.value || "";

  const [form, setForm] = useState({
    name: "",
    session_provider: validProvider,
    provider_api_key: "",
    provider_project_id: "",
    provider_host: initialHost,
    github_repo: "",
    github_token: "",
    detection_threshold: 5,
    min_sessions_threshold: 2,
    skip_page_patterns: [] as string[],
  });

  // Validation state
  const [posthogStatus, setPosthogStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [posthogMessage, setPosthogMessage] = useState("");
  const [githubStatus, setGithubStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [githubMessage, setGithubMessage] = useState("");

  const selectedProvider = PROVIDERS.find((p) => p.id === form.session_provider) || PROVIDERS[0];

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target;
    const type = (e.target as HTMLInputElement).type;
    setForm((prev) => ({
      ...prev,
      [name]: type === "number" ? parseInt(value) || 0 : value,
    }));
    // Reset validation when credentials change
    if (["provider_api_key", "provider_project_id", "provider_host"].includes(name)) {
      setPosthogStatus("idle");
    }
    if (["github_repo", "github_token"].includes(name)) {
      setGithubStatus("idle");
    }
  };

  const handleProviderChange = (providerId: string) => {
    const prov = PROVIDERS.find((p) => p.id === providerId);
    setForm((prev) => ({
      ...prev,
      session_provider: providerId,
      provider_host: prov?.hosts?.[0]?.value || "",
      provider_api_key: "",
      provider_project_id: "",
    }));
    setPosthogStatus("idle");
  };

  const handleTestPosthog = async () => {
    if (!form.provider_api_key || !form.provider_project_id) return;
    setPosthogStatus("loading");
    setPosthogMessage("");
    try {
      const result = await validatePosthog({
        api_key: form.provider_api_key,
        project_id: form.provider_project_id,
        host: form.provider_host || "eu.posthog.com",
      });
      if (result.valid) {
        setPosthogStatus("success");
        setPosthogMessage("Connected!");
      } else {
        setPosthogStatus("error");
        setPosthogMessage(result.error || "Connection failed");
      }
    } catch {
      setPosthogStatus("error");
      setPosthogMessage("Could not validate credentials");
    }
  };

  const handleTestGithub = async () => {
    if (!form.github_repo || !form.github_token) return;
    setGithubStatus("loading");
    setGithubMessage("");
    try {
      const result = await validateGithub({
        repo: form.github_repo,
        token: form.github_token,
      });
      if (result.valid) {
        setGithubStatus("success");
        setGithubMessage(result.repo_name ? `Connected to ${result.repo_name}` : "Connected!");
      } else {
        setGithubStatus("error");
        setGithubMessage(result.error || "Connection failed");
      }
    } catch {
      setGithubStatus("error");
      setGithubMessage("Could not validate credentials");
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const project = await createProject(form);
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create project");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-lg mx-auto">
      <div className="mb-8">
        <h1 className="text-[22px] font-bold text-slate-900">New Project</h1>
        <p className="text-sm text-slate-500 mt-1">Connect your session replay tool and start detecting bugs automatically.</p>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-xl mb-6 text-sm">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-5">
        <div>
          <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">
            Project Name
          </label>
          <input
            type="text"
            name="name"
            value={form.name}
            onChange={handleChange}
            required
            className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
            placeholder="My Web App"
          />
        </div>

        {/* Session Replay Provider Selector */}
        <div>
          <label className="block text-[13px] font-semibold text-slate-700 mb-2">
            Session Replay Provider
          </label>
          <div className="grid grid-cols-2 gap-2">
            {PROVIDERS.map((p) => {
              const isComingSoon = "comingSoon" in p && p.comingSoon;
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => !isComingSoon && handleProviderChange(p.id)}
                  disabled={isComingSoon}
                  className={`flex items-center gap-2.5 px-4 py-3 rounded-xl border text-sm font-medium transition relative ${
                    isComingSoon
                      ? "border-slate-100 bg-slate-50 text-slate-400 cursor-not-allowed opacity-60"
                      : form.session_provider === p.id
                        ? "border-indigo-400 bg-indigo-50 text-indigo-700 ring-2 ring-indigo-500/20"
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <span
                    className="w-3 h-3 rounded-full flex-shrink-0"
                    style={{ backgroundColor: p.color, opacity: isComingSoon ? 0.4 : 1 }}
                  />
                  {p.name}
                  {isComingSoon && (
                    <span className="ml-auto text-[10px] font-semibold bg-slate-100 text-slate-500 px-1.5 py-0.5 rounded-full">
                      Soon
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {/* Provider Config Card */}
        <div className={`p-5 ${selectedProvider.bg} border ${selectedProvider.border} rounded-2xl space-y-4`}>
          <div className="flex items-center gap-2 mb-1">
            <span
              className="w-4 h-4 rounded flex-shrink-0"
              style={{ backgroundColor: selectedProvider.color }}
            />
            <span className="text-[13px] font-semibold text-slate-700">{selectedProvider.name}</span>
          </div>

          {/* Setup Instructions */}
          <SetupInstructions type="posthog" />

          <div>
            <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">API Key</label>
            <input
              type="password"
              name="provider_api_key"
              value={form.provider_api_key}
              onChange={handleChange}
              required
              className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              placeholder={selectedProvider.apiKeyPlaceholder}
            />
          </div>
          <div>
            <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">
              {selectedProvider.projectIdLabel}
            </label>
            <input
              type="text"
              name="provider_project_id"
              value={form.provider_project_id}
              onChange={handleChange}
              required
              className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              placeholder={selectedProvider.projectIdPlaceholder}
            />
          </div>
          {selectedProvider.hasHostSelector && selectedProvider.hosts && (
            <div>
              <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">Region / Host</label>
              <select
                name="provider_host"
                value={form.provider_host}
                onChange={handleChange}
                className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              >
                {selectedProvider.hosts.map((h) => (
                  <option key={h.value} value={h.value}>{h.label}</option>
                ))}
              </select>
            </div>
          )}

          {/* Test Connection Button */}
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleTestPosthog}
              disabled={posthogStatus === "loading" || !form.provider_api_key || !form.provider_project_id}
              className="text-xs font-semibold px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {posthogStatus === "loading" ? "Testing..." : "Test Connection"}
            </button>
            <ValidationBadge status={posthogStatus} message={posthogMessage} />
          </div>
        </div>

        {/* GitHub Section */}
        <div className="p-5 bg-slate-50/50 border border-slate-200 rounded-2xl space-y-4">
          <div className="flex items-center gap-2 mb-1">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
              <rect width="24" height="24" rx="6" fill="#1b1f23" />
              <path d="M12 6C8.686 6 6 8.686 6 12c0 2.65 1.718 4.9 4.104 5.693.3.055.41-.13.41-.29 0-.142-.006-.52-.009-1.02-1.669.362-2.02-.805-2.02-.805-.273-.693-.666-.878-.666-.878-.545-.372.041-.365.041-.365.602.042.919.618.919.618.535.917 1.403.652 1.745.499.054-.388.21-.652.38-.802-1.332-.152-2.733-.666-2.733-2.965 0-.655.234-1.19.618-1.61-.062-.151-.268-.762.058-1.588 0 0 .504-.161 1.65.615a5.74 5.74 0 011.502-.202c.51.002 1.023.069 1.502.202 1.145-.776 1.648-.615 1.648-.615.328.826.122 1.437.06 1.588.385.42.617.955.617 1.61 0 2.305-1.404 2.812-2.74 2.96.215.186.407.553.407 1.114 0 .804-.007 1.453-.007 1.65 0 .161.108.348.413.29C16.284 16.897 18 14.648 18 12c0-3.314-2.686-6-6-6z" fill="white" />
            </svg>
            <span className="text-[13px] font-semibold text-slate-700">GitHub</span>
            <span className="text-[11px] text-slate-400 font-normal">(optional)</span>
          </div>
          <p className="text-xs text-slate-400 -mt-2">Connect GitHub to auto-create issues for detected bugs. You can configure this later.</p>

          {/* Setup Instructions */}
          <SetupInstructions type="github" />

          <div>
            <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">Repository</label>
            <input
              type="text"
              name="github_repo"
              value={form.github_repo}
              onChange={handleChange}
              className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              placeholder="owner/repo"
            />
          </div>
          <div>
            <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">Personal Access Token</label>
            <input
              type="password"
              name="github_token"
              value={form.github_token}
              onChange={handleChange}
              className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              placeholder="ghp_..."
            />
          </div>

          {/* Test Connection Button */}
          {(form.github_repo || form.github_token) && (
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={handleTestGithub}
                disabled={githubStatus === "loading" || !form.github_repo || !form.github_token}
                className="text-xs font-semibold px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {githubStatus === "loading" ? "Testing..." : "Test Connection"}
              </button>
              <ValidationBadge status={githubStatus} message={githubMessage} />
            </div>
          )}
        </div>

        <div>
          <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">
            Detection Threshold
          </label>
          <input
            type="number"
            name="detection_threshold"
            value={form.detection_threshold}
            onChange={handleChange}
            min={1}
            max={100}
            className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
          />
          <p className="text-xs text-slate-400 mt-1.5">
            Minimum occurrences before flagging as anomaly
          </p>
        </div>

        <div>
          <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">
            Min Sessions Threshold
          </label>
          <input
            type="number"
            name="min_sessions_threshold"
            value={form.min_sessions_threshold}
            onChange={handleChange}
            min={1}
            max={50}
            className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
          />
          <p className="text-xs text-slate-400 mt-1.5">
            Minimum sessions where the same issue must occur before it gets flagged (default: 2)
          </p>
        </div>

        <div>
          <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">
            Skip Page Patterns <span className="text-slate-400 font-normal">(optional)</span>
          </label>
          <input
            type="text"
            placeholder="/auth/callback, /oauth, /redirect"
            value={(form.skip_page_patterns || []).join(", ")}
            onChange={(e) => {
              const patterns = e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean);
              setForm((f) => ({ ...f, skip_page_patterns: patterns }));
            }}
            className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
          />
          <p className="text-xs text-slate-400 mt-1.5">
            Comma-separated URL patterns to skip in flow analysis (e.g. /auth/callback, /sso). Default auth patterns are always included.
          </p>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-indigo-600 text-white py-2.5 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition shadow-lg shadow-indigo-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? (
            <span className="flex items-center justify-center gap-2">
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Creating...
            </span>
          ) : (
            "Create Project"
          )}
        </button>
      </form>
    </div>
  );
}
