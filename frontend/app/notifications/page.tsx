"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { listProjects, getNotificationSettings, updateNotificationSettings, testNotification } from "@/lib/api";
import { Project, NotificationSettings } from "@/lib/types";

export default function NotificationsPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectParam = searchParams.get("project");

  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>(projectParam || "");
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => {
    if (!authLoading && !user) router.push("/login");
  }, [user, authLoading, router]);

  useEffect(() => {
    if (!user) return;
    listProjects()
      .then((projs) => {
        setProjects(projs);
        if (!selectedProjectId && projs.length > 0) {
          const cached = typeof window !== "undefined" ? localStorage.getItem("selectedProjectId") : null;
          setSelectedProjectId(cached && projs.some((p) => p.id === cached) ? cached : projs[0].id);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [user]);

  useEffect(() => {
    if (!selectedProjectId) return;
    setSettings(null);
    getNotificationSettings(selectedProjectId)
      .then(setSettings)
      .catch(() => {
        setSettings({ email_enabled: true, email_address: null, slack_enabled: false, slack_webhook_url: null });
      });
  }, [selectedProjectId]);

  const handleSave = async () => {
    if (!settings || !selectedProjectId) return;
    setSaving(true);
    try {
      await updateNotificationSettings(selectedProjectId, settings);
    } catch {}
    finally { setSaving(false); }
  };

  const handleTest = async () => {
    if (!selectedProjectId) return;
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testNotification(selectedProjectId);
      setTestResult(Object.entries(res.results).map(([k, v]) => `${k}: ${v}`).join(", "));
    } catch {
      setTestResult("Failed to send test");
    } finally {
      setTesting(false);
    }
  };

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

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-[22px] font-bold text-slate-900">Notifications</h1>
        <p className="text-sm text-slate-500 mt-1">Configure how you get notified when bugs are detected.</p>
      </div>

      {/* Project selector */}
      {projects.length > 1 && (
        <div className="mb-6">
          <label className="block text-[13px] font-semibold text-slate-700 mb-1.5">Project</label>
          <select
            value={selectedProjectId}
            onChange={(e) => setSelectedProjectId(e.target.value)}
            className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
      )}

      {!settings ? (
        <div className="flex items-center justify-center py-16">
          <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Email */}
          <div className="bg-white border border-slate-200/80 rounded-2xl p-6 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-indigo-100 flex items-center justify-center">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="2" y="4" width="20" height="16" rx="2" />
                    <path d="M22 7l-10 6L2 7" />
                  </svg>
                </div>
                <div>
                  <h3 className="text-[15px] font-semibold text-slate-900">Email Notifications</h3>
                  <p className="text-xs text-slate-500 mt-0.5">Get bug alerts sent to your inbox</p>
                </div>
              </div>
              <button
                onClick={() => setSettings({ ...settings, email_enabled: !settings.email_enabled })}
                className={`relative rounded-full transition-colors ${settings.email_enabled ? "bg-indigo-600" : "bg-slate-200"}`}
                style={{ width: 40, height: 22 }}
              >
                <span
                  className={`absolute top-0.5 left-0.5 bg-white rounded-full shadow transition-transform ${settings.email_enabled ? "translate-x-[18px]" : "translate-x-0"}`}
                  style={{ width: 18, height: 18 }}
                />
              </button>
            </div>
            {settings.email_enabled && (
              <input
                type="email"
                value={settings.email_address || ""}
                onChange={(e) => setSettings({ ...settings, email_address: e.target.value })}
                placeholder="notifications@company.com"
                className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              />
            )}
          </div>

          {/* Slack */}
          <div className="bg-white border border-slate-200/80 rounded-2xl p-6 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-purple-100 flex items-center justify-center">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                    <rect width="24" height="24" rx="6" fill="#4A154B" />
                    <path d="M9 14.5a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0zm1 0a1.5 1.5 0 013 0V18a1.5 1.5 0 01-3 0v-3.5zm4-5a1.5 1.5 0 110-3 1.5 1.5 0 010 3zm0 1a1.5 1.5 0 010 3H10.5a1.5 1.5 0 010-3H14zm-5 4a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm-1-1a1.5 1.5 0 01-3 0V10a1.5 1.5 0 013 0v3.5zm5-4a1.5 1.5 0 110-3 1.5 1.5 0 010 3z" fill="white" />
                  </svg>
                </div>
                <div>
                  <h3 className="text-[15px] font-semibold text-slate-900">Slack Notifications</h3>
                  <p className="text-xs text-slate-500 mt-0.5">Get bug alerts in your Slack channel</p>
                </div>
              </div>
              <button
                onClick={() => setSettings({ ...settings, slack_enabled: !settings.slack_enabled })}
                className={`relative rounded-full transition-colors ${settings.slack_enabled ? "bg-indigo-600" : "bg-slate-200"}`}
                style={{ width: 40, height: 22 }}
              >
                <span
                  className={`absolute top-0.5 left-0.5 bg-white rounded-full shadow transition-transform ${settings.slack_enabled ? "translate-x-[18px]" : "translate-x-0"}`}
                  style={{ width: 18, height: 18 }}
                />
              </button>
            </div>
            {settings.slack_enabled && (
              <input
                type="url"
                value={settings.slack_webhook_url || ""}
                onChange={(e) => setSettings({ ...settings, slack_webhook_url: e.target.value })}
                placeholder="https://hooks.slack.com/services/..."
                className="w-full border border-slate-200 bg-white rounded-xl px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 outline-none transition"
              />
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-3">
            <button
              onClick={handleSave}
              disabled={saving}
              className="bg-indigo-600 text-white px-5 py-2.5 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition shadow-md shadow-indigo-500/15 disabled:opacity-50"
            >
              {saving ? "Saving..." : "Save Settings"}
            </button>
            <button
              onClick={handleTest}
              disabled={testing}
              className="bg-white border border-slate-200 text-slate-700 px-5 py-2.5 rounded-xl text-sm font-semibold hover:bg-slate-50 transition disabled:opacity-50"
            >
              {testing ? "Sending..." : "Send Test"}
            </button>
            {testResult && <span className="text-xs text-slate-500">{testResult}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
