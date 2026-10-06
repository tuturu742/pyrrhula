import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

/**
 * Model and participant text rendered as Markdown -- bold, lists, code, links, tables.
 *
 * Raw HTML in the source is never rendered (react-markdown escapes it unless a rehype-raw
 * plugin is added, and none is), and link URLs go through react-markdown's default
 * transform, which drops `javascript:` and other unsafe schemes. Model output is untrusted
 * text; keep it that way.
 *
 * Styling is compact on purpose: this sits inside chat bubbles and transcript rows, where
 * headings the size of a page title would be wrong. Block elements get small vertical gaps
 * and nothing else.
 */
const components: Components = {
  p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-1.5 list-disc space-y-0.5 pl-5 first:mt-0 last:mb-0">{children}</ul>,
  ol: ({ children }) => (
    <ol className="my-1.5 list-decimal space-y-0.5 pl-5 first:mt-0 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  h1: ({ children }) => <p className="my-1.5 font-bold first:mt-0">{children}</p>,
  h2: ({ children }) => <p className="my-1.5 font-bold first:mt-0">{children}</p>,
  h3: ({ children }) => <p className="my-1.5 font-bold first:mt-0">{children}</p>,
  h4: ({ children }) => <p className="my-1.5 font-semibold first:mt-0">{children}</p>,
  h5: ({ children }) => <p className="my-1.5 font-semibold first:mt-0">{children}</p>,
  h6: ({ children }) => <p className="my-1.5 font-semibold first:mt-0">{children}</p>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-1.5 border-l-2 border-border pl-3 text-muted-foreground">{children}</blockquote>
  ),
  hr: () => <hr className="my-2 border-border" />,
  pre: ({ children }) => (
    <pre className="my-1.5 overflow-x-auto rounded bg-background/70 p-2 font-mono text-[0.85em] first:mt-0 last:mb-0">
      {children}
    </pre>
  ),
  code: ({ children, className }) => (
    <code className={cn("rounded bg-background/70 px-1 py-px font-mono text-[0.9em]", className)}>{children}</code>
  ),
  table: ({ children }) => (
    <div className="my-1.5 overflow-x-auto">
      <table className="border-collapse text-[0.95em]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-border px-2 py-0.5 text-left font-medium">{children}</th>,
  td: ({ children }) => <td className="border border-border px-2 py-0.5 align-top">{children}</td>,
};

export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("break-words [&_pre_code]:bg-transparent [&_pre_code]:p-0", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
