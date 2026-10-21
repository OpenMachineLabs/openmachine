---
name: motion-studio
description: Research, art-direct, build, review, and deliver original editorial technical or product films using Remotion and a selected visual style package.
---
# Motion Studio

A compact production Skill, not a renderer or autonomous agent. The host agent owns research, editorial decisions, and factual responsibility. The style package owns visual rules. An ordinary Remotion project owns animation and export.

## Workflow
1. Establish one brief with audience, platform, runtime, language, central question, source anchors, caveats, and sound intent. Separate measured findings from illustrative examples and proposed behavior.
2. Inspect a few original references and relevant instructions. Read references/sources.md and the selected style's references.md. Record date/version, material inspected, adoption, and reuse terms. A thumbnail is not evidence of motion or sound.
3. Render two materially different keyframes before code: one mechanism-first, one consequence-first. Change visual premise and hierarchy, not merely palette. Select with delegated judgment if the user delegated the choice, and record why.
4. Build and review an 8–10 second moving section that demonstrates the central transformation or relationship. Check type at intended size, object continuity, pacing, and sound. Refine before extending.
5. Build the whole film in ordinary React/TypeScript and Remotion. Each shot answers a question and changes understanding. Keep objects recognizable. Put copy, timing, colors, and paths in readable constants/props for targeted edits.
6. Render the full film, poster, and contact sheet. Watch the whole film at normal speed, intended size, with sound. Check clipping, readability, visual logic, pacing, factual overreach, transitions, and accidental silence. Fix substantive issues and review again. A successful render is not visual acceptance.
7. Deliver editable source and lockfile, poster, captions when needed, source/reproduction notes, and the actual review render/contact sheet. Keep caches, node_modules, research downloads, and routine renders untracked. State exactly what was reviewed and remaining limitations.

Ask only when missing information materially affects claims, rights, audience, or essential direction. Never fabricate footage, quotes, evaluation results, rights, or review evidence.

## Production rules
- Remotion is the sole backend. Use documented APIs and Studio; no scene DSL, custom renderer, provider framework, or second backend.
- Pin project-local dependencies and commit the lockfile. No hosted service or paid media dependency is required. Remotion is source-available under proprietary terms, not OSI open source; check current license eligibility. Individuals and teams/organizations up to three people may qualify for its free license, while organizations of four or more generally need a company license.
- Make the story legible without sound. Use original/cleared sound, authorized speech, or intentional silence; do not add a short cue and accidentally leave the rest silent.
- Reuse the existing anthropic-style-illustrations Skill when useful, adapting meaningful SVG layers for motion. A dense raster with pan/zoom is not a mechanism explanation.
- Motion reveals an action, relationship, or state change. Cuts are valid. Camera moves, cards, charts, and diagrams are tools, not defaults or taboos.
- Attribute examples and distinguish sourced measurements from illustrative data. Preserve test scope and denominator beside reported results.
- anthropic-editorial is unofficial and Anthropic-inspired, not a brand reproduction or endorsement. Do not redistribute campaigns, proprietary fonts, or unlicensed music.

## Package map
- styles/anthropic-editorial/STYLE.md: visual grammar.
- styles/anthropic-editorial/references.md: selected references and transfer notes.
- references/workflow.md: story and review workflow.
- references/remotion.md: implementation/export notes.
- references/sources.md: bounded comparison, versions/dates, adoption, and licensing.
- examples/pilot-film/: editable proof project, brief, lockfile, and original source assets.
