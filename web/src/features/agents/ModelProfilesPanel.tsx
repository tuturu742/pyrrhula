import { useState } from "react";
import { toast } from "sonner";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * D1.5: tenant-wide model profiles (provider/model/params/fallback/credentials).
 * "Key entry -> masked forever after": the create/edit form has an `api_key` input, but
 * no response from the backend ever carries a key or ciphertext back -- only the opaque
 * `credential_ref`, rendered here as a fixed "key on file" badge, never the value itself.
 */
export function ModelProfilesPanel() {
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);

  const { data: profiles, isLoading } = useQuery({
    queryKey: ["model-profiles"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });

  const archive = useMutation({
    mutationFn: async (profileId: string) => {
      const { error } = await apiClient.DELETE("/model-profiles/{agent_id}", {
        params: { path: { agent_id: profileId } },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["model-profiles"] }),
  });

  const testSaved = useMutation({
    mutationFn: async (agentId: string) => {
      const { data, error } = await apiClient.POST("/model-profiles/{agent_id}/test", {
        params: { path: { agent_id: agentId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      const d = data as { ok?: boolean; detail?: string };
      if (d.ok) toast.success(d.detail ?? "Connection OK.");
      else toast.error(d.detail ?? "Connection failed.");
    },
    onError: () => toast.error("Test failed to run."),
  });

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium">Model profiles</h2>
        <button
          type="button"
          onClick={() => setShowCreateForm((v) => !v)}
          className="rounded-md border border-border px-3 py-1.5 text-sm"
        >
          {showCreateForm ? "Cancel" : "New model profile"}
        </button>
      </div>

      {showCreateForm && (
        <ModelProfileForm
          existingProfiles={profiles ?? []}
          onSaved={() => {
            setShowCreateForm(false);
            void queryClient.invalidateQueries({ queryKey: ["model-profiles"] });
          }}
        />
      )}

      <GateModelPicker profiles={profiles ?? []} />

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {profiles?.length === 0 && (
        <p className="text-sm text-muted-foreground">No model profiles yet.</p>
      )}
      <ul className="flex flex-col gap-2">
        {profiles?.map((profile) => (
          <li key={profile.id} className="rounded-md border border-border p-3">
            <div className="mb-2 flex justify-end">
              <button
                type="button"
                className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
                disabled={testSaved.isPending}
                onClick={() => testSaved.mutate(profile.id)}
              >
                Test
              </button>{" "}
              <ConfirmButton
                title="Archive"
                description={`Archive model profile "${profile.name}"? It leaves the picker; agents pinned to it keep their reference. Reversible, not a permanent delete.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archive.mutate(profile.id)}
              >
                <button
                type="button"
                disabled={archive.isPending}
                className="rounded-md border border-destructive/50 px-2 py-0.5 text-xs text-destructive disabled:opacity-50"
              >
                Archive
              </button>
              </ConfirmButton>
            </div>
            <ModelProfileForm
              existingProfile={profile}
              existingProfiles={profiles}
              onSaved={() => void queryClient.invalidateQueries({ queryKey: ["model-profiles"] })}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

interface ModelProfileFormProps {
  existingProfile?: {
    id: string;
    name: string;
    provider: string;
    model: string;
    params: Record<string, unknown>;
    credential_ref: string | null;
    api_base: string | null;
    fallback_agent_id: string | null;
  };
  existingProfiles: Array<{ id: string; name: string }>;
  onSaved: () => void;
}

function ModelProfileForm({ existingProfile, existingProfiles, onSaved }: ModelProfileFormProps) {
  const isEditing = existingProfile !== undefined;
  const [name, setName] = useState(existingProfile?.name ?? "");
  const [provider, setProvider] = useState(existingProfile?.provider ?? "ollama");
  // The dropdown choice. "openai-compatible" stores provider="openai" + a required base
  // URL (DeepSeek, Groq, Mistral, xAI, OpenRouter, vLLM, LM Studio…); "other" keeps the
  // raw string editable (LiteLLM accepts many more prefixes; also used by test doubles).
  const [providerChoice, setProviderChoice] = useState(() => {
    const p = existingProfile?.provider ?? "ollama";
    if (p === "openai" && existingProfile?.api_base) return "openai-compatible";
    return ["ollama", "openai", "anthropic", "gemini"].includes(p) ? p : "other";
  });
  const [model, setModel] = useState(existingProfile?.model ?? "");
  const [temperature, setTemperature] = useState(
    String(existingProfile?.params.temperature ?? ""),
  );
  const [topP, setTopP] = useState(String(existingProfile?.params.top_p ?? ""));
  const [maxTokens, setMaxTokens] = useState(String(existingProfile?.params.max_tokens ?? ""));
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState(existingProfile?.api_base ?? "");
  const [fallbackProfileId, setFallbackProfileId] = useState(
    existingProfile?.fallback_agent_id ?? "",
  );

  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);

  // #2: test the form's own values before saving; when editing and the key field is blank,
  // the backend falls back to the stored key via model_profile_id.
  const testConnection = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/model-profiles/test-connection", {
        body: {
          provider,
          model,
          api_base: apiBase || null,
          api_key: apiKey || undefined,
          model_profile_id: existingProfile?.id,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => setTestResult(data ?? null),
  });

  // #3: ask the provider what models it offers. Cloud providers need the key from the
  // form (or, when editing, the profile's stored key via model_profile_id).
  const fetchModels = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/model-profiles/available-models", {
        body: {
          provider,
          api_base: apiBase || null,
          api_key: apiKey || undefined,
          model_profile_id: existingProfile?.id,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setAvailableModels(data?.models ?? []);
      if (data && data.models.length === 0 && data.detail) setTestResult({ ok: false, detail: data.detail });
    },
  });

  const capabilitiesQuery = useQuery({
    queryKey: ["model-capabilities", provider, model],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles/capabilities", {
        params: { query: { provider, model } },
      });
      if (error) throw error;
      return data;
    },
    enabled: provider.trim() !== "" && model.trim() !== "",
  });

  const save = useMutation({
    mutationFn: async () => {
      const params: Record<string, unknown> = {};
      if (temperature !== "") params.temperature = Number(temperature);
      if (topP !== "") params.top_p = Number(topP);
      if (maxTokens !== "") params.max_tokens = Number(maxTokens);

      if (isEditing) {
        const { data, error } = await apiClient.PATCH("/model-profiles/{agent_id}", {
          params: { path: { agent_id: existingProfile.id } },
          body: {
            name,
            provider,
            model,
            params,
            api_key: apiKey || undefined,
            // Always sent (even empty -> null): unlike api_key, an empty field here is
            // a real instruction ("clear the override"), not "leave unchanged".
            api_base: apiBase || null,
            fallback_agent_id: fallbackProfileId || undefined,
          },
        });
        if (error) throw error;
        return data;
      }
      const { data, error } = await apiClient.POST("/model-profiles", {
        body: {
          name,
          provider,
          model,
          params,
          api_key: apiKey || undefined,
          api_base: apiBase || undefined,
          fallback_agent_id: fallbackProfileId || undefined,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setApiKey("");
      onSaved();
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !provider.trim() || !model.trim()) return;
    save.mutate();
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2">
      <div className="grid grid-cols-3 gap-2">
        <Field label="Name">
          <input
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field
          label="Provider"
          hint={
            providerChoice === "openai-compatible"
              ? "Endpoint URL required — e.g. DeepSeek https://api.deepseek.com/v1, OpenRouter https://openrouter.ai/api/v1, Groq https://api.groq.com/openai/v1, or a local vLLM/LM Studio."
              : providerChoice === "other"
                ? "Raw LiteLLM provider prefix (advanced)."
                : undefined
          }
        >
          <select
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={providerChoice}
            onChange={(e) => {
              const choice = e.target.value;
              setProviderChoice(choice);
              if (choice === "openai-compatible") setProvider("openai");
              else if (choice !== "other") setProvider(choice);
            }}
          >
            <option value="ollama">Ollama (local)</option>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="gemini">Google Gemini</option>
            <option value="openai-compatible">OpenAI-compatible (custom URL)</option>
            <option value="other">Other…</option>
          </select>
          {providerChoice === "other" && (
            <input
              className="mt-1 w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder='LiteLLM prefix, e.g. "mistral", "groq", "echo"'
              required
            />
          )}
        </Field>
        <Field label="Model">
          <div className="flex gap-1">
            <input
              list={`models-${existingProfile?.id ?? "new"}`}
              className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={
                { openai: "gpt-4o", anthropic: "claude-sonnet-4-5", gemini: "gemini-2.5-pro",
                  "openai-compatible": "deepseek-chat", ollama: "qwen3:8b" }[providerChoice] ?? ""
              }
              required
            />
            <button
              type="button"
              onClick={() => fetchModels.mutate()}
              disabled={fetchModels.isPending || !provider.trim()}
              title="List models the provider offers (cloud providers need the API key)"
              className="shrink-0 rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
            >
              {fetchModels.isPending ? "…" : "Fetch"}
            </button>
            <datalist id={`models-${existingProfile?.id ?? "new"}`}>
              {availableModels.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </div>
        </Field>
      </div>

      <Field
        label={providerChoice === "openai-compatible" ? "Endpoint URL (required)" : "Endpoint URL"}
        hint={
          providerChoice === "openai-compatible"
            ? "The OpenAI-compatible base URL, e.g. https://api.deepseek.com/v1"
            : 'Optional. For a non-default deployment, e.g. "http://localhost:11434" for a second local Ollama instance. Leave blank to use the provider default.'
        }
      >
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={apiBase}
          onChange={(e) => setApiBase(e.target.value)}
          placeholder={
            providerChoice === "openai-compatible"
              ? "https://api.deepseek.com/v1"
              : "http://localhost:11434"
          }
          required={providerChoice === "openai-compatible"}
        />
      </Field>

      {capabilitiesQuery.data && (
        <div className="flex gap-3 text-xs text-muted-foreground">
          <span>tools: {capabilitiesQuery.data.supports_tools ? "yes" : "no"}</span>
          <span>json mode: {capabilitiesQuery.data.supports_json_mode ? "yes" : "no"}</span>
          <span>
            prompt caching: {capabilitiesQuery.data.supports_prompt_caching ? "yes" : "no"}
          </span>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2">
        <Field label="Temperature">
          <input
            type="number"
            step="0.1"
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={temperature}
            onChange={(e) => setTemperature(e.target.value)}
          />
        </Field>
        <Field label="top_p">
          <input
            type="number"
            step="0.05"
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={topP}
            onChange={(e) => setTopP(e.target.value)}
          />
        </Field>
        <Field label="max_tokens">
          <input
            type="number"
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={maxTokens}
            onChange={(e) => setMaxTokens(e.target.value)}
          />
        </Field>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Field
          label={isEditing ? "Replace API key" : "API key"}
          hint={
            isEditing
              ? existingProfile.credential_ref
                ? "A key is on file -- leave blank to keep it."
                : "No key on file."
              : "Stored encrypted; never shown again after this."
          }
        >
          <input
            type="password"
            autoComplete="new-password"
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={
              isEditing && existingProfile.credential_ref ? "•••••••• (on file)" : undefined
            }
          />
        </Field>
        <Field label="Fallback profile">
          <select
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={fallbackProfileId}
            onChange={(e) => setFallbackProfileId(e.target.value)}
          >
            <option value="">(none)</option>
            {existingProfiles
              .filter((p) => p.id !== existingProfile?.id)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
          </select>
        </Field>
      </div>

      {save.error !== null && (
        <p className="text-xs text-destructive">Failed to save model profile.</p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={save.isPending}
          className="self-start rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          {isEditing ? "Save changes" : "Create model profile"}
        </button>
        <button
          type="button"
          disabled={testConnection.isPending || !provider.trim() || !model.trim()}
          onClick={() => {
            setTestResult(null);
            testConnection.mutate();
          }}
          title="Send a one-word probe with these settings (works before saving)"
          className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
        >
          {testConnection.isPending ? "Testing…" : "Test connection"}
        </button>
      </div>
      {testResult && (
        <p className={`text-xs ${testResult.ok ? "text-green-600" : "text-destructive"}`}>
          {testResult.ok ? "✓ " : "✗ "}
          {testResult.detail}
        </p>
      )}
      {testConnection.error !== null && (
        <p className="text-xs text-destructive">Failed to reach the test-connection endpoint.</p>
      )}
    </form>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </label>
  );
}


/**
 * Which connection runs the disclosure gate for this organization.
 *
 * A tenant decision, not a deployment one: a deployment hosts many organizations and they
 * do not share a model choice. The gate is a strict-JSON classifier over gists, so what it
 * needs is structured-output support and speed, not the weight class your characters talk
 * on — and pointing it away from a large resident model also stops a one-second judgement
 * queueing behind it.
 */
function GateModelPicker({
  profiles,
}: {
  profiles: Array<{ id: string; name: string; provider?: string; model?: string }>;
}) {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["gate-model"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles/gate");
      if (error) throw error;
      return data;
    },
  });
  const save = useMutation({
    mutationFn: async (connection_id: string | null) => {
      const { error } = await apiClient.PUT("/model-profiles/gate", {
        body: { connection_id },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Gate model updated.");
      void queryClient.invalidateQueries({ queryKey: ["gate-model"] });
    },
    onError: () => toast.error("Only an owner or admin can change the gate model."),
  });

  if (profiles.length === 0) return null;
  const current = data?.connection_id ?? "";

  return (
    <div className="flex flex-col gap-1 rounded-md border border-border/60 bg-muted/30 p-3">
      <label className="text-sm font-medium" htmlFor="gate-model">
        Secret disclosure gate
      </label>
      <select
        id="gate-model"
        className="rounded-md border border-input bg-transparent px-2 py-1.5 text-sm"
        value={current}
        onChange={(e) => save.mutate(e.target.value || null)}
        disabled={save.isPending}
      >
        <option value="">Each persona&apos;s own connection</option>
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {p.model ? ` (${p.model})` : ""}
          </option>
        ))}
      </select>
      <p className="text-xs text-muted-foreground">
        The gate decides, per turn, whether a character may conceal, hint at, or reveal
        what they know. It needs a model that supports structured output — one that does
        not will make the gate fail closed and conceal everything. A small, fast model is
        the right choice here; your characters can keep talking on a different one.
      </p>
    </div>
  );
}
