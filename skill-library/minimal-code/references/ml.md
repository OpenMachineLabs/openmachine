# ML, numerical and data-pipeline code

A change that keeps a shape but changes its meaning is a bug. Before editing,
list which of these the change touches and preserve each explicitly:

- **Tensors:** shape, axis order, layout, dtype, device, padding, masks,
  sentinel values, length accounting and shifts.
- **Training:** train versus eval or generation mode, targets, causal order,
  loss masking and reduction, gradient flow, precision and numerical safety
  (empty masks, division by zero, overflow).
- **State:** parameter registration and freezing, optimizer and scheduler step
  order, gradient accumulation, distributed ownership, RNG and checkpoint
  resume.
- **Caches and recurrence:** state lifecycle, reset, session isolation and
  parity with a full-computation oracle.
- **Data contracts:** preprocessing, tokenizer or codec identity,
  serialization, configuration and checkpoint compatibility across producers
  and consumers.

Build the smallest correct vertical path with a trustworthy oracle first, such
as the plain loop or the full uncached computation. Keep the parity check against
it, including edge cases such as empty masks, as a repository test rather than
a throwaway script.
Add caching, batching, fusion or distributed execution only after a current
caller or measurement requires it. Vectorize Python-side tensor loops when
semantics are preserved.

When a published implementation is a useful reference, pin its exact version
and reuse the smallest compatible invariant or call shape, not its whole
architecture. The current repository stays authoritative.

CPU, import or unit success does not prove GPU execution, distributed
correctness, training stability, model quality or performance. Report those as
`NOT RUN` unless you ran them.
