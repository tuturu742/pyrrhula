interface CelEditorProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  errors: string[];
  placeholder?: string;
}

/**
 * F3.11's CEL editor: a plain textarea (no syntax highlighting -- nothing in this repo
 * ships a CEL grammar for CodeMirror/Monaco, and D7's own boundary is "no user code, CEL
 * expressions only", not "a bespoke editor experience") plus the live, expression-
 * anchored error list the page-level debounced `/entities/schemas/validate` call
 * produces. The page owns validation timing; this component only renders whatever
 * `errors` it's handed for its own expression's field path.
 */
export function CelEditor({ label, value, onChange, errors, placeholder }: CelEditorProps) {
  const hasErrors = errors.length > 0;
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <textarea
        rows={2}
        value={value}
        placeholder={placeholder ?? "fields.xp / 100"}
        onChange={(e) => onChange(e.target.value)}
        className={`rounded-md border bg-transparent px-2 py-1 font-mono text-xs ${
          hasErrors ? "border-destructive" : "border-input"
        }`}
      />
      {hasErrors && (
        <ul className="flex flex-col gap-0.5">
          {errors.map((message, i) => (
            <li key={i} className="text-xs text-destructive">
              {message}
            </li>
          ))}
        </ul>
      )}
    </label>
  );
}
