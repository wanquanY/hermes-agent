---
name: figma
description: Use Figma MCP for design inspection and design-to-code.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Figma, MCP, Design, Design-to-Code, FigJam, Code-Connect]
    related_skills: [native-mcp, computer-use]
---

# Figma

Use Figma through its MCP tools, not by scraping the public Figma web page.
The supported default integration is the local Figma Desktop server installed
with `hermes mcp install figma-desktop`.

## Preconditions

Before attempting a Figma operation:

1. Look for MCP tools owned by `figma-desktop` or a user-configured Figma MCP.
2. If none exist, explain that Figma Desktop must be running with its MCP
   server enabled, then direct the user to `hermes mcp install figma-desktop`.
3. For a link-based task, preserve the full Figma URL. The MCP server extracts
   its file and node identity; do not replace it with a guessed node ID.
4. For selection-based Desktop workflows, ask the user to select the exact
   frame or layer when the target is ambiguous.

## Read-first workflow

For inspection or design-to-code:

1. Call metadata or design-context tools before requesting screenshots.
2. Retrieve variable definitions and Code Connect mappings when components or
   tokens are present.
3. Use screenshots as visual verification, not as the only source of layout,
   typography, variants, or component semantics.
4. Inspect the destination codebase for its framework, component library,
   tokens, and responsive conventions before generating code.
5. Prefer mapped code components over newly invented lookalikes.
6. Verify the implementation against both structured context and screenshot.

## Mutating workflows

Canvas edits, file creation, asset upload, diagrams, and Code Connect writes are
mutating operations. Use them only when the user explicitly requests the
corresponding change and the MCP tool is enabled.

Before mutation:

1. Inspect the target file, page, frame, libraries, variables, and component
   conventions.
2. State the intended target and scope when more than one file or frame could
   match.
3. Reuse library components, variables, and Auto Layout; do not flatten a
   design into arbitrary rectangles or hard-coded values.
4. Re-read metadata or context and take a screenshot to verify the result.

Never enable additional mutating MCP tools automatically. Tell the user to
review the discovered list with `hermes mcp configure figma-desktop`.

## Design-to-code quality bar

- Treat Figma as design intent plus structured constraints, not production
  source code.
- Preserve semantic components, variants, responsive behavior, accessibility,
  and existing repository architecture.
- Use Code Connect mappings when available; they outrank visual inference.
- Resolve discrepancies in this order: explicit user direction, Code Connect,
  variables/components, structured design context, screenshot inference.
- Do not introduce a parallel design system when the codebase already has one.

## Fallback

Computer Use may operate the native Figma UI only when the MCP server cannot
perform a required UI-specific action. It is a fallback for interaction, not a
replacement for structured design context.
