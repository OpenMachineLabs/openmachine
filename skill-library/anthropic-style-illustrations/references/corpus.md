# Corpus and provenance

The current corpus contains 913 documents, 3,858 figure occurrences and 3,697
asset records. The original 912-document corpus is preserved with one manually
registered PDF source and three page/region occurrences added. Classification
currently yields 849 source publications, 62 incidental supporting records, one
landing page and one explicitly verified official paper. See [coverage](coverage.md)
for measured case/family counts and limitations. The 59 analyses preserve the
supplied source reviewer inspection notes, not newly manufactured reviews.

`articles.jsonl` retains URLs, title/description excerpts, source/response hashes,
version metadata and links. `figures.jsonl` identifies occurrences, article/asset
relationships, short captions, context and DOM/PDF locators. `assets.jsonl`
deduplicates normalized URLs or known embedded-content hashes. External images
are not downloaded; different URLs containing identical bytes may therefore
remain distinct assets. Repeated occurrences of one asset are deduplicated in
retrieval results, while their source-bound IDs remain available via `show`.

The collector imposes a shared 150-word excerpt budget per article. It keeps no
full source articles, source PDFs, remote scripts or original image binaries.
PDF records are caption candidates, not verified crops. Media extraction supports
lazy URLs, image proxies, inline SVG, embedded raster metadata, video/canvas,
interactive embeds and dynamic locators. It is not a rendered-browser crawler.
Dynamic pages, absent captions, download failures and client-only pagination
remain coverage limitations. No bulk network refresh was performed. Bounded web context was read; the requested
SafeFetcher execution was blocked by local DNS and native tool approval policy.

A single-writer advisory lock protects each collector store. Upserts/deletes and
source state enter an fsynced metadata journal before application. Snapshot writes
are atomic per file; replay reconciles a partial snapshot idempotently. Only an
unterminated malformed final journal line can be treated as a torn write. Complete
malformed transactions fail closed. Preserve the journal after a write failure.
A failed refresh retains old records and is retried; an orphan HTTP 304 does not
count as collection. Discovery rescans explicit roots. Default CLI limits bound
work; linked PDFs have a separate 0–100 fanout cap and extraction rejects more
than 500 pages or 2,000 caption candidates.

Direct HTTPS fetching pins a checked public DNS address for the TCP connection
while verifying TLS against the original hostname. Host/port/redirect/byte/robots
limits remain enforced. The `system` mode delegates transport and DNS
to the managed OS/proxy. The bounded `publication-context` command uses this mode. Do not use that mode
for untrusted network routing. No network transport acceptance is claimed by
fixture-only tests.

Public availability is not redistribution permission. Preserve rights evidence
and attribution; prefer links and original explanations. Do not infer an image
license from an adjacent repository or dataset license. The added patterns are
original design guidance, not corpus observations or an official brand manual.

Manual PDF page/region occurrences survive collector refresh even when caption
extraction does not rediscover them. Their analyses still stale when the source
fingerprint changes. Q1–Q3 currently bind actual web-extracted text lines 0–91,
not a full PDF byte hash; this limitation is explicit on the source record.
Official-paper provenance requires both an official companion and authorship
evidence; an arbitrary arXiv citation is insufficient. No source-media binaries
or full source articles are part of these deliverables.
