"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Eye, EyeOff, Loader2, Save, Star, Trash2 } from "lucide-react";
import {
  formatKnowledgeTimestamp,
  isMarginNoteKb,
  providerUsesEmbeddingMetadata,
  type KnowledgeBase,
} from "@/lib/knowledge-helpers";

interface KbSettingsSectionProps {
  kb: KnowledgeBase;
  onSetDefault: () => Promise<void>;
  onDelete: () => Promise<void>;
  onSave: (config: Record<string, unknown>) => Promise<void>;
}

export default function KbSettingsSection({
  kb,
  onSetDefault,
  onDelete,
  onSave,
}: KbSettingsSectionProps) {
  const { t } = useTranslation();
  const meta = kb.metadata || {};
  // A MarginNote library runs no engine and no embedding, and its `path` is
  // a name rather than a folder that exists — reporting the ordinary fields
  // described a pipeline and a directory it never has.
  const isMarginNote = isMarginNoteKb(kb);
  const provider = isMarginNote
    ? t("MarginNote 4")
    : kb.statistics?.rag_provider || "llamaindex";
  const pageIndexProvider =
    isMarginNote || !providerUsesEmbeddingMetadata(provider);
  const embeddingLabel = meta.embedding_model
    ? typeof meta.embedding_dim === "number"
      ? `${meta.embedding_model} · ${meta.embedding_dim}${t("d")}`
      : meta.embedding_model
    : t("Default embedding");
  const created = formatKnowledgeTimestamp(meta.created_at);
  const updated = formatKnowledgeTimestamp(meta.last_updated);
  const lastIndexed = formatKnowledgeTimestamp(meta.last_indexed_at);

  // ── Editable settings ──────────────────────────────────────────
  const [description, setDescription] = useState(meta.description || "");
  const [serverUrl, setServerUrl] = useState(meta.server_url || "");
  const [apiKey, setApiKey] = useState(meta.api_key || "");
  const [showApiKey, setShowApiKey] = useState(false);
  const [searchMode, setSearchMode] = useState(meta.search_mode || "");
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<"ok" | "error" | null>(null);

  // Sync local state when kb changes (e.g. list refresh)
  useEffect(() => {
    setDescription(meta.description || "");
    setServerUrl(meta.server_url || "");
    setApiKey(meta.api_key || "");
    setSearchMode(meta.search_mode || "");
    setSaveResult(null);
  }, [kb.name, meta.description, meta.server_url, meta.api_key, meta.search_mode]);

  const hasServerUrl = ["lightrag-server", "weknora", "ima"].includes(provider);
  const hasSearchMode = provider === "pageindex" || provider === "pageindex-oss";

  const isDirty =
    description !== (meta.description || "") ||
    serverUrl !== (meta.server_url || "") ||
    apiKey !== (meta.api_key || "") ||
    searchMode !== (meta.search_mode || "");

  const handleSave = useCallback(async () => {
    if (!isDirty || saving) return;
    setSaving(true);
    setSaveResult(null);
    try {
      const patch: Record<string, unknown> = {};
      if (description !== (meta.description || "")) {
        patch.description = description;
      }
      if (hasServerUrl && serverUrl !== (meta.server_url || "")) {
        patch.server_url = serverUrl;
      }
      if (hasServerUrl && apiKey !== (meta.api_key || "")) {
        patch.api_key = apiKey;
      }
      if (hasSearchMode && searchMode !== (meta.search_mode || "")) {
        patch.search_mode = searchMode;
      }
      await onSave(patch);
      setSaveResult("ok");
    } catch {
      setSaveResult("error");
    } finally {
      setSaving(false);
    }
  }, [
    description,
    serverUrl,
    apiKey,
    searchMode,
    isDirty,
    saving,
    meta.description,
    meta.server_url,
    meta.api_key,
    meta.search_mode,
    hasServerUrl,
    hasSearchMode,
    onSave,
  ]);

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div>
          <div className="text-[13px] font-medium text-[var(--foreground)]">
            {t("Overview")}
          </div>
          <p className="mt-0.5 text-[11.5px] text-[var(--muted-foreground)]">
            {t("Read-only metadata. Use the actions below to manage this KB.")}
          </p>
        </div>

        <dl className="grid gap-3 rounded-lg border border-[var(--border)] bg-[var(--background)] p-3 sm:grid-cols-2">
          <Field label={t("RAG provider")}>{provider}</Field>
          {!pageIndexProvider && (
            <Field label={t("Embedding")}>{embeddingLabel}</Field>
          )}
          <Field label={t("Created")}>{created || "—"}</Field>
          <Field label={t("Updated")}>{updated || "—"}</Field>
          {!isMarginNote && (
            <Field label={t("Last indexed")}>{lastIndexed || "—"}</Field>
          )}
          {isMarginNote
            ? meta.db_path && (
                <Field label={t("Synced store")} className="sm:col-span-2">
                  <span className="font-mono text-[10.5px] text-[var(--muted-foreground)]">
                    {meta.db_path}
                  </span>
                </Field>
              )
            : kb.path && (
                <Field label={t("On-disk path")} className="sm:col-span-2">
                  <span className="font-mono text-[10.5px] text-[var(--muted-foreground)]">
                    {kb.path}
                  </span>
                </Field>
              )}
        </dl>
      </section>

      {/* ── Editable settings ──────────────────────────────────── */}
      <section className="space-y-3">
        <div>
          <div className="text-[13px] font-medium text-[var(--foreground)]">
            {t("Settings")}
          </div>
          <p className="mt-0.5 text-[11.5px] text-[var(--muted-foreground)]">
            {t("Editable configuration for this knowledge base.")}
          </p>
        </div>

        <div className="space-y-3 rounded-lg border border-[var(--border)] bg-[var(--background)] p-3">
          {/* Description */}
          <div>
            <label className="mb-1 block text-[10.5px] uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
              {t("Description")}
            </label>
            <input
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder={t("e.g. Research papers on machine learning")}
                className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1.5 text-[12.5px] text-[var(--foreground)] outline-none transition-colors focus:border-[var(--foreground)]/25"
            />
          </div>

          {/* Server URL — only for providers that use an external service */}
          {hasServerUrl && (
              <>
                <div>
                  <label className="mb-1 block text-[10.5px] uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
                    {t("Server URL")}
                  </label>
                  <input
                      value={serverUrl}
                      onChange={(event) => setServerUrl(event.target.value)}
                      placeholder={t("https://...")}
                      className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1.5 font-mono text-[12px] text-[var(--foreground)] outline-none transition-colors focus:border-[var(--foreground)]/25"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[10.5px] uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
                    {t("API key")}
                  </label>
                  <div className="relative">
                    <input
                        type={showApiKey ? "text" : "password"}
                        value={apiKey}
                        onChange={(event) => setApiKey(event.target.value)}
                        placeholder={t("sk-...")}
                        className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1.5 pr-8 font-mono text-[12px] text-[var(--foreground)] outline-none transition-colors focus:border-[var(--foreground)]/25"
                    />
                    <button
                        type="button"
                        onClick={() => setShowApiKey(!showApiKey)}
                        className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-[var(--muted-foreground)] transition-colors hover:text-[var(--foreground)]"
                        tabIndex={-1}
                    >
                      {showApiKey ? (
                          <EyeOff className="h-3.5 w-3.5" />
                      ) : (
                          <Eye className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>
                </div>
              </>
          )}

          {/* Search mode — only for PageIndex providers */}
          {hasSearchMode && (
              <div>
                <label className="mb-1 block text-[10.5px] uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
                  {t("Search mode")}
                </label>
                <select
                    value={searchMode}
                    onChange={(event) => setSearchMode(event.target.value)}
                    className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1.5 text-[12.5px] text-[var(--foreground)] outline-none transition-colors focus:border-[var(--foreground)]/25"
                >
                  <option value="">{t("Default")}</option>
                  <option value="flash">{t("Flash")}</option>
                  <option value="standard">{t("Standard")}</option>
                </select>
              </div>
          )}

          {/* Save button + feedback */}
          <div className="flex items-center gap-2 pt-1">
            <button
                type="button"
                onClick={() => void handleSave()}
                disabled={!isDirty || saving}
                className="inline-flex items-center gap-1.5 rounded-md bg-[var(--primary)] px-2.5 py-1.5 text-[12px] font-medium text-[var(--primary-foreground)] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {saving ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                  <Save className="h-3 w-3" />
              )}
              {t("Save")}
            </button>
            {saveResult === "ok" && (
                <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600">
                <Check className="h-3 w-3" />
                  {t("Saved")}
              </span>
            )}
            {saveResult === "error" && (
                <span className="text-[11px] text-red-600">
                {t("Failed to save")}
              </span>
            )}
          </div>
        </div>
      </section>

      <section className="space-y-3 rounded-lg border border-[var(--border)] bg-[var(--background)] p-3">
        <div>
          <div className="text-[12.5px] font-medium text-[var(--foreground)]">
            {t("Default knowledge base")}
          </div>
          <p className="mt-0.5 text-[11.5px] text-[var(--muted-foreground)]">
            {t("The default KB is selected automatically in chat & partners.")}
          </p>
        </div>
        {kb.is_default ? (
          <span className="inline-flex items-center gap-1.5 rounded-md bg-amber-100 px-2.5 py-1 text-[12px] font-medium text-amber-700 dark:bg-amber-950/30 dark:text-amber-300">
            <Star className="h-3 w-3" fill="currentColor" />
            {t("Currently default")}
          </span>
        ) : (
          <button
            type="button"
            onClick={() => void onSetDefault()}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1 text-[12px] font-medium text-[var(--foreground)] transition-colors hover:bg-[var(--muted)]"
          >
            <Star className="h-3 w-3" />
            {t("Set as default")}
          </button>
        )}
      </section>

      <section className="space-y-3 rounded-lg border border-red-200 bg-red-50/40 p-3 dark:border-red-900/60 dark:bg-red-950/15">
        <div>
          <div className="text-[12.5px] font-medium text-red-700 dark:text-red-300">
            {t("Danger zone")}
          </div>
          <p className="mt-0.5 text-[11.5px] text-red-700/80 dark:text-red-300/80">
            {isMarginNote
              ? t(
                  "Deleting this library removes its synced objects and unpairs every device. Nothing in MarginNote 4 itself is touched.",
                )
              : t(
                  "Deleting a knowledge base permanently removes its raw documents and index versions.",
                )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void onDelete()}
          className="inline-flex items-center gap-1.5 rounded-md border border-red-300 bg-red-50 px-2.5 py-1 text-[12px] font-medium text-red-700 transition-colors hover:bg-red-100 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300 dark:hover:bg-red-950/50"
        >
          <Trash2 className="h-3 w-3" />
          {t("Delete knowledge base")}
        </button>
      </section>
    </div>
  );
}

function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <dt className="text-[10.5px] uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
        {label}
      </dt>
      <dd className="mt-1 text-[12.5px] text-[var(--foreground)]">
        {children}
      </dd>
    </div>
  );
}
