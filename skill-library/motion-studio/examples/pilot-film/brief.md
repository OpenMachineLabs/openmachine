# Pilot brief — Context is part of retrieval

- Audience: engineers and technical product teams familiar with search, not necessarily RAG internals.
- Destination: public 16:9 short; silent viewing must still communicate the mechanism.
- Language/runtime: English, 40 seconds, 1920×1080, 30 fps.
- Central question: Why can a relevant passage become hard to retrieve after a document is split into chunks?
- Source: Anthropic, “Introducing Contextual Retrieval,” published 2024-09-19: https://www.anthropic.com/research/contextual-retrieval
- Sourced mechanism: traditional chunking can remove document/time context; Contextual Retrieval prepends concise chunk-specific context before creating both embeddings and BM25 index.
- Sourced result: in Anthropic's top-20-chunk evaluation, contextual embeddings reduced retrieval failure from 5.7% to 3.7%; contextual embeddings plus contextual BM25 reduced it from 5.7% to 2.9%, a 49% relative reduction. This is not a universal guarantee.
- Illustrative visual: “The company's revenue grew by 3% over the previous quarter” and ACME/Q2 2023 context are adapted from the article's example, not a new finding.
- Direction A selected: a sentence strip is detached from its filing; a leaf-green context tab attaches to the same strip, which forks into semantic and lexical indexes. The object persists so the mechanism is legible.
- Direction B considered: begin with a query scanning filing fragments, then reveal the ambiguous fragment as the culprit. Rejected because it makes search UI, rather than the chunk's lost identity, the protagonist.
- Sound: original generated low-volume paper/wood percussion and warm sustained tones; no speech, stock music, or external audio.
- Captions: not required; all key information is on-screen and there is no narration.
- Claims boundary: relative failure reduction from the article's described setup; no claim about downstream tasks or all production corpora.
