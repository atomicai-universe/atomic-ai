"use client";

import * as React from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";

import { cn } from "@/lib/utils";
import {
  registerVoiceEditor,
  replaceTextCI,
  type VoiceEditorController,
} from "@/lib/voice-editor-registry";

/**
 * Imperative surface the RichTextEditor exposes to its parent (mirrors the old
 * textarea ref usage: read the current value on save, focus programmatically).
 */
export interface RichTextEditorHandle {
  /** Current plain-text content (newline-joined paragraphs). */
  getText: () => string;
  /** Replace the whole content with plain text. */
  setText: (text: string) => void;
  /** Focus the editor (move the caret in). */
  focus: () => void;
  /** Case-insensitive find-and-replace over the plain text; returns replaced?. */
  replaceText: (search: string, replacement: string) => boolean;
}

interface RichTextEditorProps {
  /** Initial plain-text value. */
  value: string;
  /** Called with plain text on every edit so a parent can mirror state. */
  onChange?: (text: string) => void;
  /** Disable editing (whole editor becomes read-only + dimmed). */
  disabled?: boolean;
  /**
   * Logical voice-field name to register under (e.g. "reply_body") so the voice
   * action layer can dictate / set / find-and-replace into THIS editor. Also
   * mirrored onto the DOM as `data-voice-field` for parity with native fields.
   */
  voiceField?: string;
  className?: string;
  ariaLabel?: string;
}

/**
 * Headless TipTap (ProseMirror) plain-text editor.
 *
 * Why TipTap: the reply body needs to be PROGRAMMATICALLY editable by voice —
 * insert text, set the whole field, and find-and-replace a phrase — which a
 * plain <textarea> cannot do cleanly. TipTap gives a real document model and a
 * transaction API, so voice edits are precise and never clobber the caret.
 *
 * Plain-text contract: the backend stores/sends the reply body as text/plain
 * (MIMEText "plain"), so this editor reads and writes PLAIN TEXT only. We use
 * StarterKit for solid editing/keymaps but serialize with `editor.getText()`
 * (newline-joined) and load text as paragraphs — no HTML crosses the boundary.
 *
 * SSR: `immediatelyRender: false` avoids a Next.js hydration mismatch (TipTap
 * must render only on the client).
 */
export const RichTextEditor = React.forwardRef<RichTextEditorHandle, RichTextEditorProps>(
  function RichTextEditor(
    { value, onChange, disabled = false, voiceField, className, ariaLabel },
    ref,
  ) {
    // Keep the latest onChange in a ref so the editor's onUpdate closure stays
    // stable without re-creating the editor.
    const onChangeRef = React.useRef(onChange);
    React.useEffect(() => {
      onChangeRef.current = onChange;
    }, [onChange]);

    const editor = useEditor({
      immediatelyRender: false,
      editable: !disabled,
      extensions: [StarterKit],
      content: textToDoc(value),
      editorProps: {
        attributes: {
          // Parity with native voice fields + a11y. The voice layer prefers the
          // registry, but the attribute keeps the field discoverable in the DOM.
          ...(voiceField ? { "data-voice-field": voiceField } : {}),
          role: "textbox",
          "aria-multiline": "true",
          ...(ariaLabel ? { "aria-label": ariaLabel } : {}),
          class: cn(
            "prose-none min-h-32 w-full rounded-md border bg-background px-3 py-2 " +
              "text-sm leading-relaxed focus:outline-none focus-visible:ring-2 " +
              "focus-visible:ring-ring focus-visible:ring-offset-2",
            disabled && "cursor-not-allowed opacity-50",
            className,
          ),
        },
      },
      onUpdate: ({ editor: ed }) => {
        onChangeRef.current?.(ed.getText());
      },
    });

    // Keep editability in sync when `disabled` toggles.
    React.useEffect(() => {
      editor?.setEditable(!disabled);
    }, [editor, disabled]);

    // Build the imperative controller once the editor exists and register it so
    // the voice layer can drive it. Re-registers if the editor instance changes.
    React.useEffect(() => {
      if (!editor) return;
      const controller: VoiceEditorController = {
        getText: () => editor.getText(),
        setText: (text: string) => {
          editor.commands.setContent(textToDoc(text));
          onChangeRef.current?.(editor.getText());
        },
        focus: () => {
          try {
            editor.commands.focus("end");
          } catch {
            /* focus can throw in odd states — ignore */
          }
        },
        replaceText: (search: string, replacement: string) => {
          const [next, replaced] = replaceTextCI(editor.getText(), search, replacement);
          if (!replaced) return false;
          editor.commands.setContent(textToDoc(next));
          onChangeRef.current?.(editor.getText());
          return true;
        },
      };
      // Expose to the parent via the forwarded ref too.
      if (typeof ref === "function") ref(controller);
      else if (ref) ref.current = controller;

      if (!voiceField) return;
      const unregister = registerVoiceEditor(voiceField, controller);
      return unregister;
    }, [editor, voiceField, ref]);

    return <EditorContent editor={editor} />;
  },
);

/**
 * Convert plain text to a ProseMirror doc JSON: each line becomes a paragraph
 * (an empty line becomes an empty paragraph). Pure and DOM-free.
 */
function textToDoc(text: string): {
  type: "doc";
  content: Array<{ type: "paragraph"; content?: Array<{ type: "text"; text: string }> }>;
} {
  const lines = (text ?? "").split("\n");
  return {
    type: "doc",
    content: lines.map((line) =>
      line.length > 0
        ? { type: "paragraph", content: [{ type: "text", text: line }] }
        : { type: "paragraph" },
    ),
  };
}
