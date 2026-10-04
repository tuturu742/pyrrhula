import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { ModelPicker } from "@/features/agents/ModelProfilesPanel";

/**
 * The connection the deployment's own assistant talks to.
 *
 * The same form a tenant gets for a model profile — provider dropdown, a model field that
 * can ask the provider what it offers, a test before saving — because the first version
 * was four bare text boxes and an operator who had just used the tenant form found it
 * "terrible". Both forms run the same probes server-side; this one falls back to the
 * stored key when the key field is blank, exactly as editing a saved profile does.
 */

const KNOWN = ["ollama", "openai", "anthropic", "gemini"];

function choiceFor(provider: string, apiBase: string | null | undefined): string {
  if (provider === "openai" && apiBase) return "openai-compatible";
  if (!provider) return "ollama";
  return KNOWN.includes(provider) ? provider : "other";
}

export function AssistantConnectionCard() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["admin-assistant-model"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/assistant/model");
      if (error) throw error;
      return data;
    },
  });

  const [provider, setProvider] = useState("ollama");
  // The dropdown choice. "openai-compatible" stores provider="openai" + a required base
  // URL (DeepSeek, Groq, Mistral, xAI, OpenRouter, vLLM, LM Studio…); "other" keeps the
  // raw string editable — the same three-way split the tenant form makes.
  const [providerChoice, setProviderChoice] = useState("ollama");
  const [model, setModel] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);

  useEffect(() => {
    if (!data) return;
    setProvider(data.provider || "ollama");
    setProviderChoice(choiceFor(data.provider, data.api_base));
    setModel(data.model ?? "");
    setApiBase(data.api_base ?? "");
  }, [data]);

  const hasStoredConnection = Boolean(data?.provider);

  const fetchModels = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/admin/assistant/model/available-models", {
        body: { provider, model, api_base: apiBase || null, api_key: apiKey || undefined },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      setAvailableModels(data?.models ?? []);
      if (data && data.models.length === 0 && data.detail) {
        setTestResult({ ok: false, detail: data.detail });
      }
    },
    onError: () => toast.error("Could not reach the model-listing endpoint."),
  });

  const testConnection = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/admin/assistant/model/test", {
        body: { provider, model, api_base: apiBase || null, api_key: apiKey || undefined },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => setTestResult(data ?? null),
    onError: () => toast.error("Could not reach the test-connection endpoint."),
  });

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/assistant/model", {
        body: {
          provider: provider.trim(),
          model: model.trim(),
          api_base: apiBase || null,
          api_key: apiKey || null,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setApiKey("");
      toast.success("Assistant model saved.");
      void queryClient.invalidateQueries({ queryKey: ["admin-assistant-model"] });
    },
    onError: () => toast.error("Could not save the assistant model."),
  });

  const inputClass =
    "w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60";
  const ready = provider.trim() !== "" && model.trim() !== "";

  return (
    <form
      className="flex flex-col gap-3 rounded-md border border-border p-4"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div>
        <h2 className="text-sm font-medium">Assistant model</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          The connection the app&apos;s own assistant answers from. It is separate from any
          organization&apos;s model profiles, so no workspace budget pays for it.
        </p>
      </div>

      {isLoading ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : (
        <>
          <div className="grid gap-3 md:grid-cols-2">
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
                className={inputClass}
                value={providerChoice}
                onChange={(e) => {
                  const choice = e.target.value;
                  setProviderChoice(choice);
                  setAvailableModels([]);
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
                  className={`mt-1 ${inputClass}`}
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  placeholder='LiteLLM prefix, e.g. "mistral", "groq", "echo"'
                  required
                />
              )}
            </Field>
          </div>

          <Field
            label={
              providerChoice === "openai-compatible" ? "Endpoint URL (required)" : "Endpoint URL"
            }
            hint={
              providerChoice === "openai-compatible"
                ? "The OpenAI-compatible base URL, e.g. https://api.deepseek.com/v1"
                : 'Optional. For a non-default deployment, e.g. "http://localhost:11434" for a second local Ollama instance. Leave blank to use the provider default.'
            }
          >
            <input
              className={inputClass}
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

          <Field
            label="API key"
            hint={
              hasStoredConnection
                ? "Leave blank to keep the key on file. It is never shown back."
                : "Local providers need none."
            }
          >
            <input
              className={inputClass}
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={hasStoredConnection ? "(unchanged)" : ""}
            />
          </Field>

          {/* After the key: listing a cloud provider's models needs it. */}
          <Field label="Model">
            <ModelPicker
              value={model}
              onChange={setModel}
              models={availableModels}
              placeholder={
                {
                  openai: "gpt-4o",
                  anthropic: "claude-sonnet-4-5",
                  gemini: "gemini-2.5-pro",
                  "openai-compatible": "deepseek-chat",
                  ollama: "qwen3:8b",
                }[providerChoice] ?? ""
              }
              fetching={fetchModels.isPending}
              onFetch={() => fetchModels.mutate()}
              fetchBlockedReason={
                !provider.trim()
                  ? "Choose a provider first"
                  : providerChoice !== "ollama" && !apiKey && !hasStoredConnection
                    ? "Enter the API key first"
                    : null
              }
            />
          </Field>

          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={save.isPending || !ready}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={testConnection.isPending || !ready}
              title="Send a one-word probe with these settings (works before saving)"
              onClick={() => {
                setTestResult(null);
                testConnection.mutate();
              }}
            >
              {testConnection.isPending ? "Testing…" : "Test connection"}
            </Button>
          </div>
          {testResult && (
            <p className={`text-xs ${testResult.ok ? "text-green-600" : "text-destructive"}`}>
              {testResult.ok ? "✓ " : "✗ "}
              {testResult.detail}
            </p>
          )}
        </>
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
