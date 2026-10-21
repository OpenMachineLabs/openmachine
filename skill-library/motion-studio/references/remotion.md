# Remotion production notes

The pilot uses Remotion 4 with React and TypeScript. Use official Remotion Agent Skills/docs for API-specific behavior; this is a local workflow note, not a copy of upstream docs.

- One Composition declares width, height, fps, and durationInFrames.
- Drive animation from useCurrentFrame/useVideoConfig; avoid wall-clock state, CSS transitions, and non-deterministic randomness.
- Use interpolate/spring with explicit ranges and clamping. Hold settled states long enough to read.
- Use Sequence or explicit frame ranges. Render stills and the full film from the same composition.
- Preview in Studio, render with the project-local CLI, inspect the file with ffprobe or equivalent.
- Pin remotion, @remotion/cli, @remotion/renderer, @remotion/bundler, react, react-dom and typescript.
- From examples/pilot-film: npm ci; npm run studio; npm run render; npm run still; npm run typecheck.
- Renders and generated soundtrack live in ignored out/public paths. The soundtrack generator is tracked, its generated output is not.
- For a targeted copy/timing change, edit scene constants, render a still, then rerender the film.

Official references: https://www.remotion.dev/docs/ai/skills and https://www.remotion.dev/docs/

## License and codec constraints

Remotion is source-available software governed by its proprietary license, not an OSI-approved open-source license. As of the research snapshot (2026-10-22), Remotion states that individuals and organizations/teams of up to three people can use the free license, while organizations of four or more generally require a company license. Eligibility depends on current terms and actual use case; verify before adoption. Remotion's current FAQ also defines programmatic calls to npx remotion render as automation: for an organization that needs a Company License, automated production may fall under Remotion for Automators at $0.01 per render with a $100/month minimum; manual production under Remotion for Creators is $25/month per seat. These fees are Remotion license terms, not an added hosted rendering service. Official terms: https://www.remotion.dev/docs/license/pricing and https://www.remotion.dev/docs/license/faq.

The pilot uses H.264 video and AAC audio in MP4 for broad compatibility. Remotion's license does not grant third-party codec patent rights. Determine whether additional codec licensing/royalties apply to your distribution and jurisdiction. No proprietary font, stock audio, external API, or paid render service is bundled.
