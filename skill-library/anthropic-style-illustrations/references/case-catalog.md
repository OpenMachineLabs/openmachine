# Reviewed reference cases

These 59 studies preserve source reviewer’s original wording from curation-01 through curation-05. Import validation checks structure and source binding; it does not endorse observations. No new pixel inspection was performed in this pass. E8 is a negative contrast, excluded from style retrieval. Q1–Q3 retain exact manual PDF regions; their source fingerprint covers retrieved text lines 0–91, not PDF bytes or all pages. A later full-PDF refresh must stale that narrower snapshot.

## A1 — Augmented model: capabilities rather than chronology

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fd3083d3f40bb2b6f477901cc9a240738d3dd1371-2401x1000.png&w=3840)

Case `A1` · Figure `figure_a6d275f9b7ac2d9cdaee3f7c` · Article `article_ffc229d83891918f82064b92`
Source hash: `2fdf06d408df1eed8110155b018b02f105c0e3174477133c4e2c931cdc889519`

Family: flow. Relationship: capability access; bidirectional interfaces. Usage: reference.

**Observed**

- Input, LLM and output form an upper horizontal spine. Retrieval, tools and memory are beneath the model.
- Dashed paired arrows distinguish query/results, call/response and read/write; the three capability cards share a supporting color.

**Source-context paraphrase**: The section introduces retrieval, tools and memory as model augmentations before the workflow gallery.

**Purpose**: Establish the primitive used in later diagrams. A prose list names facilities but does not show their relationship to one model.

**Information mapping**: The upper spine carries the user-facing exchange; the lower region is an inventory of interfaces, not three required stages. Repeated cards express comparable roles, not equal cost.

**Inferred rationale**: Separating the spine from resources plausibly prevents a false retrieval-then-tools-then-memory reading. Verb pairs make direction meaningful rather than decorative.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Lay out the spine first, then three equal capability cards. Route paired labeled edges without crossing the output path. Include a line-style key when optional access matters.

**Reusable rules**

- Separate capability inventory from runtime sequence.
- Label both directions of an interface with the different information exchanged.

**Counterexamples**

- Lower-row order must not imply mandatory serial execution.
- Card size is not evidence of resource consumption.

**Cross-domain transfer**

- Domain: Museum research
- Mapping: A curator consults an archive, examination instruments and working notes before producing a catalog entry.
- Changes: Use lookup/findings, examine/observations and consult/update verbs while retaining the output spine.
- Limits: This explains available resources, not proof that all resources were consulted.

Original note: `runs/curation-01.json#A1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A2 — A chain with an explicit failed gate

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F7418719e3dab222dccb379b8879e1dc08ad34c78-2401x1000.png&w=3840)

Case `A2` · Figure `figure_b4a60880eba23dfef0f4035d` · Article `article_ffc229d83891918f82064b92`
Source hash: `f25155bf35fbfded7ad751fc16af3b7b62c37c8f65d9821fa62e4ddaeda2b305`

Family: flow. Relationship: sequence; predicate; early exit. Usage: reference.

**Observed**

- The first call feeds a violet gate; its passing route bends upward through two later calls to the output.
- A dashed failing branch descends to a separate exit. Intermediate outputs are labeled on the main path.

**Source-context paraphrase**: Prompt chaining breaks work into dependent subtasks and permits programmatic checks between them.

**Purpose**: Show that later work is conditional on an acceptable intermediate artifact, not simply another item in a list.

**Information mapping**: Model cards produce artifacts, the gate tests a predicate, and separate endpoints denote success and termination. The displaced failure lane preserves reading order.

**Inferred rationale**: A visible negative outcome counters the common optimistic assumption that every input reaches the final call. A distinct control color separates checking from generation.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Specify the predicate, then place the successful sequence and reserve a lower exit lane. Label both branches in addition to varying stroke style. Add retries only when the actual procedure supports them.

**Reusable rules**

- Preserve the negative branch of a decision.
- Name intermediate artifacts so dependency is intelligible.

**Counterexamples**

- Independent benefits are not a serial chain.
- An unspecified decorative gate does not explain validation.

**Cross-domain transfer**

- Domain: Multilingual publishing
- Mapping: Draft, validate factual requirements, translate and prepare the localized layout.
- Changes: Use real artifacts and route rejected drafts according to the actual editorial policy.
- Limits: A passed draft check does not guarantee translation accuracy or eventual publication.

Original note: `runs/curation-01.json#A2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A3 — Routing: alternatives are not concurrent workers

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F5c0c0e9fe4def0b584c04d37849941da55e5e71c-2401x1000.png&w=3840)

Case `A3` · Figure `figure_606c4039a9aab3c910989f31` · Article `article_ffc229d83891918f82064b92`
Source hash: `40fe3fba7d6426056751e7e1e7cdf2d3d9e5449cf57ca1f1517d69b8afe01129`

Family: flow. Relationship: classification; exclusive dispatch. Usage: reference.

**Observed**

- A router leads to three vertically aligned call boxes and a shared output; there is no aggregator card.
- One route is solid and other routes are dashed. The peer-box arrangement resembles the neighboring parallelization example.

**Source-context paraphrase**: The routing section assigns inputs to specialized downstream tasks.

**Purpose**: Distinguish choosing a specialist from running all specialists for every request.

**Information mapping**: Fan-out represents possible paths. The router owns selection, handlers produce alternative results, and the common destination is not a synthesis operation.

**Inferred rationale**: Keeping the worker silhouette familiar makes the absence of an aggregator significant when the gallery is compared. Dashes need explanation because they could otherwise mean uncertainty or failure.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Label the input category at each route. Keep handler cards equal, emphasize the selected path and explain alternative routes. Include an unknown-category destination when the source specifies one.

**Reusable rules**

- Differentiate alternatives from concurrency using topology and labels.
- Use stable geometry across comparable pattern figures.

**Counterexamples**

- Do not use exclusive dispatch when every branch contributes to one result.
- Dashed routes do not encode measured probabilities.

**Cross-domain transfer**

- Domain: Support triage
- Mapping: A dispatcher chooses billing, account-access or delivery support.
- Changes: State selection criteria and preserve the service's actual escalation path.
- Limits: The diagram does not establish perfect classification or interchangeable response types.

Original note: `runs/curation-01.json#A3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A4 — Independent work followed by an explicit join

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F406bb032ca007fd1624f261af717d70e6ca86286-2401x1000.png&w=3840)

Case `A4` · Figure `figure_f2226c54393c0e981fdad4ab` · Article `article_ffc229d83891918f82064b92`
Source hash: `292c67512f6c2a633eeb379e3a64f2b3bf6088c2d88fccc4e6019a6b30e2db70`

Family: flow. Relationship: fork; parallel work; aggregation. Usage: reference.

**Observed**

- Solid connectors fan from the input into three equal green peer boxes.
- All worker outputs enter a violet aggregator before the final output. The join is a named processing card, not merely intersecting lines.

**Source-context paraphrase**: The parallelization section combines independent work performed concurrently.

**Purpose**: Externalize how separate contributions become one result. A list of subtasks would not show the synthesis obligation.

**Information mapping**: Branch position expresses peer status; edges carry artifacts and dependencies; the many-to-one card owns combination. Equal area is not equal duration or confidence.

**Inferred rationale**: The explicit aggregator makes reconciliation visible and distinguishes this figure from routing, even though both contain three worker boxes.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Identify truly independent tasks and the aggregator's input contract. Give each incoming branch an unambiguous port and name the synthesis action. Use branch artifact labels rather than unlabeled connectors.

**Reusable rules**

- Name a join when it evaluates or reconciles results.
- Verify independence before depicting parallel branches.

**Counterexamples**

- A later task that needs an earlier result belongs in a chain.
- Equal cards do not show equal effort or reliability.

**Cross-domain transfer**

- Domain: Community-event planning
- Mapping: Gather accessibility needs, venue constraints and transport options, then consolidate them into a plan.
- Changes: Describe conflict resolution at synthesis rather than treating concatenation as sufficient.
- Limits: Do not imply all evidence streams are equally trustworthy or that teams never communicate.

Original note: `runs/curation-01.json#A4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A5 — Dynamic decomposition precedes synthesis

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F8985fc683fae4780fb34eab1365ab78c7e51bc8e-2401x1000.png&w=3840)

Case `A5` · Figure `figure_cc54e65f66be5075d3a5652c` · Article `article_ffc229d83891918f82064b92`
Source hash: `780a5431fd35e63e1349c229f65a9f9f74a1962071a55d19e0a3758a043a291f`

Family: flow. Relationship: orchestration; contingent task set; synthesis. Usage: reference.

**Observed**

- A green orchestrator precedes three worker calls, with a green synthesizer after them.
- Dotted worker connections differ from the solid fixed-parallel example; coral endpoints remain consistent.

**Source-context paraphrase**: The section distinguishes dynamically determined subtasks from a predefined parallel workflow.

**Purpose**: Make the decision about what work to do visible, rather than presenting only the resulting worker inventory.

**Information mapping**: Orchestration creates a task set, workers return contributions, and synthesis makes the final artifact. Three depicted workers are illustrative cardinality, not a fixed system guarantee.

**Inferred rationale**: Separating decomposition and synthesis clarifies two different kinds of judgment. Dotted paths may suggest contingent work but cannot establish that meaning without prose.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Add a variable-task-set label and show representative workers with an ellipsis when needed. Keep decomposition and synthesis separate. Route each result explicitly rather than drawing an unexplained central cloud.

**Reusable rules**

- Show who chooses the decomposition.
- Do not confuse the depicted worker count with a runtime constraint.

**Counterexamples**

- A fixed three-step procedure is not dynamic delegation.
- One dashed-line convention should not simultaneously stand for optionality, uncertainty and asynchronous work.

**Cross-domain transfer**

- Domain: Renovation assessment
- Mapping: A surveyor chooses required structural, electrical or moisture investigations before an architect consolidates findings.
- Changes: Use property-dependent investigations, not three mandatory trades copied from the reference.
- Limits: Required professional review and approvals must remain explicit.

Original note: `runs/curation-01.json#A5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A6 — Feedback carries information, not guaranteed improvement

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F14f51e6406ccb29e695da48b17017e899a6119c7-2401x1000.png&w=3840)

Case `A6` · Figure `figure_ef124886031c1d9413654b40` · Article `article_ffc229d83891918f82064b92`
Source hash: `f0a1ad89e03fc7df0bcef5a6d6efa0834d8b71db9ff7153bcb7cd8ae20d5b587`

Family: flow. Relationship: iteration; critique; acceptance. Usage: reference.

**Observed**

- Generator and evaluator exchange a solution on an upper curved path and rejection/feedback on a lower return path.
- An accepted branch leaves toward an output distinct from the cycle.

**Source-context paraphrase**: The evaluator-optimizer pattern uses evaluation feedback to refine generated work.

**Purpose**: Explain what changes between attempts; a generic circular arrow would conceal the critique that makes revision possible.

**Information mapping**: The forward artifact and return feedback are different messages. Evaluation owns the exit decision. The cycle depicts attempts, not convergence or a measured quality trend.

**Inferred rationale**: Opposing labeled arcs reduce confusion about who produces the criterion. A visible accepted exit keeps iteration from appearing unbounded.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Define an evaluable acceptance criterion, name both exchanged artifacts and reserve an exit port. Add a retry limit or escalation only when supported by the article's actual workflow.

**Reusable rules**

- Give both directions of a feedback loop distinct labels.
- Expose termination and escalation conditions.

**Counterexamples**

- Repeated independent sampling without actionable feedback is a different pattern.
- Iteration count cannot be presented as quality improvement without evidence.

**Cross-domain transfer**

- Domain: Copy editing
- Mapping: An author submits a draft; an editor returns specific issues; a passing revision proceeds to production.
- Changes: State the rubric and route unresolved disagreements to a human decision after bounded revisions.
- Limits: Editorial acceptance is not proof of factual correctness or audience effectiveness.

Original note: `runs/curation-01.json#A6`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A7 — Environment feedback and human supervision are separate

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F58d9f10c985c4eb5d53798dea315f7bb5ab6249e-2401x1000.png&w=3840)

Case `A7` · Figure `figure_37cce56c445c5cc5989447ed` · Article `article_ffc229d83891918f82064b92`
Source hash: `9df2170a49314c3c98697c07cf2bce441c153d165e5f2ef6f92dedab9883b4fe`

Family: flow. Relationship: action; observation; supervision; stopping. Usage: reference.

**Observed**

- Human, model and environment occupy distinct positions; action and feedback connect model and environment in opposite directions.
- Dashed supervisory connections lead to the human and a dotted downward path terminates at a stop card.

**Source-context paraphrase**: The article describes agents acting through tools and environmental feedback, with opportunities for human input and stopping.

**Purpose**: Distinguish text generation from choosing actions whose observed results affect later choices.

**Information mapping**: The environment is a stateful participant. Action arrows change it; feedback returns observations; supervision is a separate control interface rather than another resource lookup.

**Inferred rationale**: Placing the human outside the main cycle can suggest intermittent oversight. That is a useful reading only when the operational policy supports it; the picture itself does not establish autonomy or safety.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: List permitted actions, observable state and stop triggers before layout. Give actions and observations separate lanes, label the human's authority and keep termination visible.

**Reusable rules**

- Draw stateful environments explicitly.
- Distinguish supervisory control from ordinary feedback.

**Counterexamples**

- A human icon is not a safety guarantee.
- A static lookup with no state-changing action does not require this control loop.

**Cross-domain transfer**

- Domain: Greenhouse irrigation
- Mapping: A controller changes valves, sensors report soil state and a grower supervises exceptions.
- Changes: Add real water limits, sensor-failure handling and manual shutdown conditions.
- Limits: A conceptual loop is not a certified engineering safety diagram.

Original note: `runs/curation-01.json#A7`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## A8 — Sequence lifelines expose responsibility and order

Source: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F4b9a1f4eb63d5962a6e1746ac26bbc857cf3474f-2400x1666.png&w=3840)

Case `A8` · Figure `figure_b8170880f759f522cffe9099` · Article `article_ffc229d83891918f82064b92`
Source hash: `6a32c667096005c6a582f1dc9a96535020aec7be134c135ddf6a7389a4964d2c`

Family: sequence. Relationship: messages; clarification loop; test loop; completion. Usage: reference.

**Observed**

- Four vertical lifelines identify human, interface, LLM and environment; labeled messages progress downward.
- Dashed enclosures distinguish clarification and test-related loops before completion/display messages.

**Source-context paraphrase**: The coding-agent example shows interaction with a user and execution environment during iterative work.

**Purpose**: Answer who sends what and when, which an architecture map cannot resolve.

**Information mapping**: Columns identify participants, vertical position expresses order, arrows identify messages and containers mark repeated subsequences. None measures duration without a time scale.

**Inferred rationale**: Fixed participant positions reduce remapping through repeated attempts. Separate loop regions prevent clarification and implementation from becoming one vague circle.

**Palette**: White canvas, pale green model rectangles, pale violet supporting/control cards, coral endpoint pills, muted gray arrows; not a beige editorial template.

**Typography**: Short centered sans-serif role labels; smaller verbs on interfaces. Exact font unknown.

**Uncertain reconstruction**: Enumerate participants and messages, then group repetitions. Allocate label width before choosing lifeline spacing. Provide failure and timeout paths when the real workflow requires them, and pair this detail with an overview.

**Reusable rules**

- Use lifelines for message responsibility and sequence.
- Keep separate iterations visibly distinct.

**Counterexamples**

- Vertical spacing is not elapsed time.
- A happy-path sequence is not a complete operational audit trail.

**Cross-domain transfer**

- Domain: Grant applications
- Mapping: Applicant, portal, reviewer and records service exchange submission, clarification and decision messages.
- Changes: Name actual documents and separate the clarification cycle from records validation.
- Limits: Do not imply every application finishes successfully or that the diagram records an actual run.

Original note: `runs/curation-01.json#A8`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## B1 — A role overview preserves the external conversation boundary

Source: [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F1198befc0b33726c45692ac40f764022f4de1bf2-4584x2579.png&w=3840)

Case `B1` · Figure `figure_52b1617aa2d9120d50cb73cd` · Article `article_a3afc1e8c7c23916e7f5233b`
Source hash: `59ab0ea7daf1ee7e76c801a411878fd38255e33f68cae18b96961427cfa52e29`

Family: network. Relationship: coordination; delegation; memory; provenance. Usage: reference.

**Observed**

- Large chat and system panels are linked by user-request and final-report arrows.
- A central lead agent connects to memory, a citation agent and three search subagents below, with return paths and self-loops.

**Source-context paraphrase**: The system uses a lead researcher, subagents, memory and citation processing.

**Purpose**: Teach the responsibilities before presenting the long execution trace; otherwise multiple agents look like arbitrary duplication.

**Information mapping**: Boundary-crossing arrows identify deliverables, spatial centrality identifies coordination, peripheral cards identify shared responsibilities. Card area does not measure compute or authority.

**Inferred rationale**: Keeping citation and memory roles visible plausibly prevents reducing the system to a search fan-out. The chat boundary makes internal exchanges distinct from user-visible outputs.

**Palette**: Warm gray/off-white grouping panels and restrained blue or green role cards; neutral message connectors.

**Typography**: Sans-serif role headings and smaller message annotations; the detailed square sequence requires a larger reading scale.

**Uncertain reconstruction**: Place the external boundary and its two artifacts first. Add the coordinator and shared services, then worker exchanges. Explain self-loops rather than using them as decorative activity indicators.

**Reusable rules**

- Separate user-facing and internal exchanges.
- Keep persistence and provenance roles visible when they affect the result.

**Counterexamples**

- Central card size is not resource consumption.
- This map does not determine message timing.

**Cross-domain transfer**

- Domain: Investigative newsroom
- Mapping: An editor coordinates reporters, consults an archive and requests source verification before publishing.
- Changes: Distinguish archival lookup from factual approval and label each reporting stream.
- Limits: A citation service cannot be drawn as automatic proof that all claims are true.

Original note: `runs/curation-01.json#B1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## B2 — Research lifecycle as an overview's detailed companion

Source: [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F3bde53c9578d74f6e05c3e515e20b910c5a8c20a-4584x4584.png&w=3840)

Case `B2` · Figure `figure_7e657e27307a52d982cbf1f7` · Article `article_a3afc1e8c7c23916e7f5233b`
Source hash: `352e598aeece84dab20bbf25f01765e613302e08616ae8df45a6c5bcca306b8e`

Family: sequence. Relationship: iterative research; persistence; final citation processing. Usage: reference.

**Observed**

- Seven lifelines separate user, system, lead, two subagents, memory and citation roles.
- A large tinted research-loop region contains repeated exchanges and self-loops; citation processing and the final report occur later.

**Source-context paraphrase**: The article explains subagent research coordination and finalization of citations and report output.

**Purpose**: Expose handoffs, continuation and finalization after readers already know the roles.

**Information mapping**: Columns remain participants; downward position orders messages; the loop groups repeated research. Persistence and finalization are separate operations rather than generic arrows into one store.

**Inferred rationale**: The enclosure makes repeated investigation legible but increases density. This is a zoomable detail view, not an appropriate small thumbnail or sole overview.

**Palette**: Warm gray/off-white grouping panels and restrained blue or green role cards; neutral message connectors.

**Typography**: Sans-serif role headings and smaller message annotations; the detailed square sequence requires a larger reading scale.

**Uncertain reconstruction**: Derive a message list, distinguish memory reads/writes and group repetitions before routing. Produce both a compact overview and larger detail rather than shrinking labels indiscriminately.

**Reusable rules**

- Separate iteration from finalization.
- Use coordinated overview/detail views at different reading scales.

**Counterexamples**

- Loop regions do not guarantee convergence.
- Spacing and panel height are not execution duration.

**Cross-domain transfer**

- Domain: Policy evidence review
- Mapping: A coordinator requests two evidence streams, stores intermediate findings, decides whether more work is needed and checks sources before delivery.
- Changes: Add the actual continuation criterion and human resolution for conflicting evidence.
- Limits: A designed lifecycle is not proof a real review followed every step.

Original note: `runs/curation-01.json#B2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## C1 — Static prompt versus managed information state

Source: [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Ffaa261102e46c7f090a2402a49000ffae18c5dd6-2292x1290.png&w=3840)

Case `C1` · Figure `figure_883c1904ab5933050a8f7e6e` · Article `article_126e07cf0d684e0333f96055`
Source hash: `64402674a64be0412d10d41181e83f857146debd64a005287bb5375f47064c47`

Family: comparison. Relationship: matched conditions; state curation; feedback. Usage: reference.

**Observed**

- The simpler left panel shows prompt-to-model-to-message. The wider right panel adds documents, tools, memory/history and a dashed current-context boundary.
- Information cards and selection marks identify curation, while tool activity returns information to history.

**Source-context paraphrase**: The section contrasts prompt writing with iterative management of all information available to an agent.

**Purpose**: Show that context engineering is more than renaming a prompt. Familiar primitives are held constant while state and selection are exposed.

**Information mapping**: Containers distinguish stored resources from selected current information. The return path changes state; it is not a second unrelated example. Extra panel width accommodates complexity rather than measuring superiority.

**Inferred rationale**: Stable model/output grammar reduces comparison effort. The added resources alone do not establish improved results, so the new design should emphasize selection rather than accumulation.

**Palette**: Warm neutral canvas with blue, green, orange and violet semantic accents; colors identify information roles rather than a universal quality rank.

**Typography**: Sans-serif labels, short headings and compact text examples; exact font and color values unmeasured.

**Uncertain reconstruction**: List stores, selection and update operations separately; reuse model geometry across panels; then add the bounded context container and return path.

**Reusable rules**

- Hold familiar components constant in a conceptual comparison.
- Distinguish available information from information actually loaded.

**Counterexamples**

- More documents do not automatically improve results.
- Wider or more colorful panels are not quantitative superiority evidence.

**Cross-domain transfer**

- Domain: Adaptive teaching
- Mapping: Compare a fixed worksheet with a teacher-curated lesson using prior work, references and current feedback.
- Changes: Show a bounded selection and subsequent update of teaching notes.
- Limits: Resource count is not a measure of learning effectiveness.

Original note: `runs/curation-01.json#C1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## C2 — Qualitative specificity calibrated through same-task examples

Source: [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F0442fe138158e84ffce92bed1624dd09f37ac46f-2292x1288.png&w=3840)

Case `C2` · Figure `figure_c9060d09f45912651692d3b6` · Article `article_126e07cf0d684e0333f96055`
Source hash: `d389f2d08c6de86088f5342efbd67a7d58bbd2677bc90d12fe1fcf5df166e3c7`

Family: concept. Relationship: ordered continuum; granularity; contrasting examples. Usage: reference.

**Observed**

- A tiled horizontal band uses orange ends and a green middle. Three markers connect to examples of different textual density.
- Labels distinguish excessive specificity, a useful middle and vagueness; no numeric axis establishes an optimum.

**Source-context paraphrase**: The article discusses a useful instruction granularity between brittle hardcoding and vague guidance.

**Purpose**: Make an abstract calibration recommendation actionable through concrete formulations of the same task.

**Information mapping**: Position conveys qualitative granularity, not a measured score. Example wording changes while task identity stays stable; tiles and spacing are not quantities.

**Inferred rationale**: Same-task examples isolate specificity from topic. Green emphasis makes the recommendation obvious but risks implying a tested universal optimum without a conceptual label.

**Palette**: Warm neutral canvas with blue, green, orange and violet semantic accents; colors identify information roles rather than a universal quality rank.

**Typography**: Sans-serif labels, short headings and compact text examples; exact font and color values unmeasured.

**Uncertain reconstruction**: Draft three genuinely different instructions for one scenario. Link each to a qualitative scale, keep text readable and supply full accessible examples outside the image when needed.

**Reusable rules**

- Calibrate with the same task at multiple levels of detail.
- Label conceptual scales explicitly.

**Counterexamples**

- Marker spacing does not quantify performance.
- A generic middle-ground rule cannot relax exact safety requirements.

**Cross-domain transfer**

- Domain: Operational procedures
- Mapping: Contrast exhaustive hardcoded branches, a principle-based checklist and vague advice in one workplace scenario.
- Changes: State which decisions the operator may exercise and which constraints remain mandatory.
- Limits: Different risk levels and audiences need different specificity; there is no universal optimum.

Original note: `runs/curation-01.json#C2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D1 — Preparation and runtime need different containers

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F45603646e979c62349ce27744a940abf30200d57-3840x2160.png&w=3840)

Case `D1` · Figure `figure_5501cdf0129159b9a3b7f592` · Article `article_33d81116743596d4b1f6f110`
Source hash: `7fade35f9e9ae20609206df353bed099efa7ab1b8758aeb1983b27f0cd3a726e`

Family: flow. Relationship: preprocessing; persistent indexes; retrieval fusion. Usage: reference.

**Observed**

- Two pale panels divide preprocessing and runtime. Documents enter embedding and TF-IDF/indexing paths that cross into retrieval and fusion.
- A query enters from above, and ranked information continues toward a model and response. Baseline processing remains neutral.

**Source-context paraphrase**: The article establishes retrieval components before explaining contextual changes.

**Purpose**: Prevent the reader from confusing reusable preparation with operations performed for each query.

**Information mapping**: Phase containers identify when work occurs; persistent indexes are the connecting artifacts; parallel retrieval channels converge before generation. A boundary is not necessarily a server or security boundary.

**Inferred rationale**: A quiet baseline leaves visual emphasis available for the intervention in the next figure. Reusing its coordinates can make a changed method easier to understand than redrawing everything.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: List persisted artifacts separately from per-request operations. Place each on the correct side, label crossing information and route the query independently of source documents.

**Reusable rules**

- Separate once-per-corpus from once-per-query work.
- Establish a restrained baseline before an intervention comparison.

**Counterexamples**

- A container does not establish a security guarantee.
- An unlabeled merge conceals ranking semantics when those semantics matter.

**Cross-domain transfer**

- Domain: Library catalog services
- Mapping: Catalog books and build indexes before a patron query finds and combines candidate records.
- Changes: Use actual catalog-index names and show the final librarian selection separately.
- Limits: The drawing explains information movement, not measured search quality.

Original note: `runs/curation-02.json#D1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D2 — Show the precise insertion point of contextual enrichment

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F2496e7c6fedd7ffaa043895c23a4089638b0c21b-3840x2160.png&w=3840)

Case `D2` · Figure `figure_a3cee6c91de1c2dede455794` · Article `article_33d81116743596d4b1f6f110`
Source hash: `e2dfc2c0cf829931fe04071ec2cfa0f5bb53d60ad2119da275f5da07ad009475`

Family: flow. Relationship: source document; fragment enrichment; unchanged indexing. Usage: reference.

**Observed**

- A wider preparation panel shows a whole document, selected blue chunks and context generation.
- Blue context-plus-chunk pairs feed otherwise neutral indexing; a small runtime region retains orientation.

**Source-context paraphrase**: The method adds context to chunks before they are indexed.

**Purpose**: Explain what contextual changes and when it changes, instead of attributing benefit to a vague new search component.

**Information mapping**: Document and fragment are separate inputs to an enrichment operation. Blue marks the intervention, and gray downstream components mark continuity with the baseline.

**Inferred rationale**: Showing the full document beside a fragment makes the missing local framing tangible. Keeping indexing recognizable isolates the change without implying a different final model.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: Draw one fragment, its source and a visibly distinct annotation. Form an enriched unit, then insert it into the unchanged pipeline. Keep original content and added interpretation separate.

**Reusable rules**

- Highlight only changed information and operations.
- Never make generated context look like a verbatim source quotation.

**Counterexamples**

- Do not move a preparation-time intervention to runtime.
- Added context is not automatic factual verification.

**Cross-domain transfer**

- Domain: Archival clipping research
- Mapping: A curator adds origin, date and topic framing to a clipping before indexing it.
- Changes: Visually separate the transcribed clipping from curator-authored metadata and preserve provenance for each.
- Limits: Annotations can be wrong; enrichment should not be drawn as a truth certificate.

Original note: `runs/curation-02.json#D2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D3 — Lower failure is better: make the direction explicit

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F7f8d739e491fe6b3ba0e6a9c74e4083d760b88c9-3840x2160.png&w=3840)

Case `D3` · Figure `figure_3799b1c8fa91e0639960cc74` · Article `article_33d81116743596d4b1f6f110`
Source hash: `f87ea527bfc9179d60bcbfa3300a3c6680e7f371018c7b17189ce51701ea5a70`

Family: bar. Relationship: matched ablation; failure rate; common baseline. Usage: reference.

**Observed**

- Four bars compare standard and contextual groups; the visible values are 5.7, 5.0, 3.7 and 2.9.
- The failure-rate scale starts at zero. A dotted divider separates condition groups, and subdued colors distinguish variants.

**Source-context paraphrase**: The article evaluates retrieval failure for contextual and non-contextual configurations.

**Purpose**: Move from a method explanation to inspectable evidence. A process diagram alone cannot demonstrate improvement.

**Information mapping**: Two conditions cross retrieval variants on one numeric scale; shorter bars represent fewer failures. Direct labels allow arithmetic verification without visually estimating small heights.

**Inferred rationale**: Keeping the unfavorable direction explicit reduces the chance that taller bars are intuitively read as better. The lack of visible intervals must not be mistaken for zero uncertainty.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: Start from an actual result table, retain a zero baseline, define the failure denominator and label the better direction. Compute percentage-point and relative changes separately from named endpoints.

**Reusable rules**

- Mark lower-is-better outcomes.
- Use a common scale for matched ablations and distinguish relative percentages from percentage points.

**Counterexamples**

- Never reuse source benchmark values as a new topic's measurements.
- Do not invent confidence intervals or significance claims.

**Cross-domain transfer**

- Domain: Manufacturing inspection
- Mapping: Compare defect rates under two inspection methods with and without structured preparation.
- Changes: Use real defect counts and denominators, and show uncertainty when available.
- Limits: Different lots or sampling conditions may invalidate a causal comparison.

Original note: `runs/curation-02.json#D3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D4 — Candidate generation is not reranking or synthesis

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F8f82c6175a64442ceff4334b54fac2ab3436a1d1-3840x2160.png&w=3840)

Case `D4` · Figure `figure_8f8ee85ad28e7e7007e77cca` · Article `article_33d81116743596d4b1f6f110`
Source hash: `f98bf1d0944407dc3f75f59541ef3d4cb9f1e9695aa5fe9e0228599899e62c2c`

Family: flow. Relationship: retrieval; fusion; reranking; selection. Usage: reference.

**Observed**

- The two-phase architecture remains recognizable, with blue contextual preparation on the left.
- A reranking stage appears between runtime fusion and final generation, exposing successive candidate-selection steps.

**Source-context paraphrase**: The article combines contextual retrieval with reranking before generation.

**Purpose**: Distinguish finding possible evidence from ordering and selecting it. Otherwise one search-to-answer arrow hides the additional mechanism.

**Information mapping**: Persistent preparation and runtime remain separate. Candidate sets move through fusion, ranking and selection; generation consumes selected evidence rather than producing the rankings itself.

**Inferred rationale**: Stable coordinates reduce relearning while a localized new box identifies the second intervention. Greater density argues for a detail view rather than shrinking all annotations.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: Reuse baseline coordinates, insert reranking with labeled input/output sets, and preserve the final model as a separate responsibility. Split candidate-count details into a zoomed panel if labels become crowded.

**Reusable rules**

- Localize an intervention within a stable mental map.
- Separate candidate production, ordering and composition.

**Counterexamples**

- A new stage is not evidence of improved accuracy.
- Reranking does not create new evidence merely by changing order.

**Cross-domain transfer**

- Domain: Policy briefing
- Mapping: Find candidate sources, combine lists, assess relevance and compose a briefing from selected material.
- Changes: Show selection criteria separately from factual verification.
- Limits: Highly ranked evidence may still be false or incomplete.

Original note: `runs/curation-02.json#D4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D5 — Keep intermediate baselines when combining improvements

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F93a70cfbb7cca35bb8d86ea0a23bdeeb699e8e58-3840x2160.png&w=3840)

Case `D5` · Figure `figure_7189eb51553985d43d0a67e7` · Article `article_33d81116743596d4b1f6f110`
Source hash: `8434197513cdd3f39e49373582c5abfa6d842012c2f927f37f546e4d8790ea67`

Family: bar. Relationship: factorial comparison; combined intervention. Usage: reference.

**Observed**

- Six bars extend the earlier chart: 5.7, 5.0 and 3.5 in the standard group; 3.7, 2.9 and 1.9 in the contextual group.
- Added reranking variants receive stronger terracotta emphasis while the original scale and ordering remain recognizable.

**Source-context paraphrase**: The combined configuration is reported alongside earlier retrieval baselines.

**Purpose**: Show incremental comparisons rather than only the most flattering best-versus-worst endpoint.

**Information mapping**: Two condition groups contain three configurations each. Repeated order enables within-group and cross-group reading; color emphasizes an added method without changing how values are encoded.

**Inferred rationale**: Preserving all tested baselines discourages attributing the entire endpoint difference to one modification. The evidence series builds on prior figures rather than silently replacing them.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: Use one tidy result table, retain the earlier axis and group order, and label any summary change with its exact endpoints. Preserve sampling caveats near the figure.

**Reusable rules**

- Retain intermediate configurations in combined-method evidence.
- Use accent color without omitting inconvenient baselines.

**Counterexamples**

- The full endpoint difference is not the isolated effect of reranking.
- Visible separation does not establish statistical significance.

**Cross-domain transfer**

- Domain: Service-request improvement
- Mapping: Compare three routing configurations before and after structured intake is added.
- Changes: Use the service's real error metric and show every tested condition.
- Limits: A changed request population or time period must be disclosed before causal language.

Original note: `runs/curation-02.json#D5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## D6 — Disaggregation with an explicit independent-scale warning

Source: [Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F646a894ec4e6120cade9951a362f685cd2ec89b2-2458x2983.png&w=3840)

Case `D6` · Figure `figure_a105958f9cbb6a0d2cadfb36` · Article `article_33d81116743596d4b1f6f110`
Source hash: `c1ecdf120e2ec0e1097354d60e215d8c9a60aa3d8a67487808f4359ee56f6f36`

Family: small_multiples. Relationship: domain heterogeneity; repeated configuration comparison. Usage: reference.

**Observed**

- Five stacked domain panels reuse a multicolor method legend and dense direct labels.
- Vertical ranges differ across panels; embedding-category labels occupy the lower portion of each repeated chart.

**Source-context paraphrase**: The appendix breaks evaluation down by domain and configuration.

**Purpose**: Reveal exceptions and domain dependence hidden by an aggregate improvement.

**Information mapping**: Panel identity encodes domain; position within each panel encodes comparable methods. Between-panel magnitudes require axis reading because scales differ.

**Inferred rationale**: Repeated grammar makes method-specific exceptions easier to spot, but local scale expansion trades absolute comparability for detail. Reuse must preserve that tradeoff rather than silently treating heights as comparable.

**Palette**: Warm off-white phase panels, neutral baseline operations, blue intervention accents and coral/terracotta evidence accents.

**Typography**: Compact sans-serif operation labels; data charts have direct numeric labels and restrained grids.

**Uncertain reconstruction**: Use a table with domain, method and result columns. Hold method order/colors constant. Prefer shared axes for magnitude comparisons; otherwise label independent scales and supply accessible data.

**Reusable rules**

- Disaggregate when transfer to a specific domain matters.
- Disclose independent scales and maintain method order.

**Counterexamples**

- Equal physical heights can represent different values across panels.
- A dense appendix chart is unsuitable as a tiny mobile hero.

**Cross-domain transfer**

- Domain: Multi-site course assessment
- Mapping: Compare instructional interventions across courses or locations with one method legend.
- Changes: Choose shared or independent axes according to the actual comparison question.
- Limits: Small multiples do not correct unequal samples or make site differences causal.

Original note: `runs/curation-02.json#D6`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E1 — A concrete console paired with an abstract test inventory

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fcdc027ad2730e4732168bb198fc9363678544f99-1920x1080.png&w=3840)

Case `E1` · Figure `figure_66ac9d60d9e9086aa53c7871` · Article `article_1399aec680791f782180f8a7`
Source hash: `768b9e760acf63af0b3d4bb6dbc17d6fce6cacbd4672be8a2689f6bb84eaeb40`

Family: screenshot. Relationship: concrete example; task coverage; one-to-many mapping. Usage: reference.

**Observed**

- A dark console occupies the left, with four stacked coral task cards on the right.
- Envelope, search, chat and trash-like symbols distinguish task cards; branching arrows connect the console context to the inventory.

**Source-context paraphrase**: The surrounding section describes evaluation as a way to test tool behavior systematically.

**Purpose**: Connect implementation work to the variety of tasks it must support, rather than leaving evaluation as an abstract recommendation.

**Information mapping**: The screenshot provides a concrete working surface; the cards summarize task categories. Their count and equal size do not encode prevalence or measured performance.

**Inferred rationale**: Mixing concrete and abstract views can make a testing taxonomy less detached from the development process. The screenshot should remain evidence, not a decorative texture.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Create an original, safe console example and a small labeled task inventory. Route each card to the relevant operation, preserve legibility and include the text outside the image.

**Reusable rules**

- Use concrete and abstract views for different explanatory jobs.
- Do not use icon count or card area as an implied metric.

**Counterexamples**

- A console screenshot is not proof the depicted evaluation succeeded.
- Do not reproduce personal identifiers or private transcripts.

**Cross-domain transfer**

- Domain: Kitchen process testing
- Mapping: Pair one preparation procedure with test cards for ordering, substitution, timing and cleanup.
- Changes: Use original neutral examples and labels, not software-specific icons copied without meaning.
- Limits: Coverage categories alone do not establish adequate testing.

Original note: `runs/curation-02.json#E1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E2 — Directly labeled held-out comparison

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F6e810aee67f3f3c955832fb7bf9033ffb0102000-1920x1080.png&w=3840)

Case `E2` · Figure `figure_9c63de9c187e78e20a3a0ff8` · Article `article_1399aec680791f782180f8a7`
Source hash: `80eb951e8899e3651544e987e7ee3b8b04542db1c4fb1c0413a18c436144173a`

Family: bar. Relationship: baseline versus optimized; held-out accuracy. Usage: reference.

**Observed**

- Two pink/coral bars labeled for human-written and optimized tools show 67.4 and 80.1.
- The vertical accuracy scale runs from zero to 100, with restrained grid lines and direct numeric labels.

**Source-context paraphrase**: The figure reports held-out performance for the Slack tool evaluation.

**Purpose**: Provide a compact result after the development method, with a visible baseline and unambiguous metric.

**Information mapping**: Category identifies tool condition; height is accuracy on a common scale. Direct values permit checking the percentage-point change rather than estimating it from area.

**Inferred rationale**: A full percentage scale avoids exaggerated visual gains. No intervals are visible, so aesthetic clarity must not become a claim that uncertainty is absent.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Use the actual held-out result table, label the dataset and denominator, retain zero, and calculate relative and percentage-point changes separately. Add intervals only if supported.

**Reusable rules**

- Identify the evaluation population and baseline.
- Keep percentage scales and arithmetic explicit.

**Counterexamples**

- Do not reuse these measurements for a different tool or dataset.
- Two bars alone do not prove generalization to all tasks.

**Cross-domain transfer**

- Domain: Document quality control
- Mapping: Compare a baseline checklist with a revised checklist on held-out documents.
- Changes: Use real scoring definitions and sampling information.
- Limits: A higher score on one held-out collection does not certify universal accuracy.

Original note: `runs/curation-02.json#E2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E3 — The paired result that preserves scale and reveals unequal gains

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F3f1f47e80974750cd924bc51e42b6df1ad997fab-1920x1080.png&w=3840)

Case `E3` · Figure `figure_9ccdd50e037550679b0e7081` · Article `article_1399aec680791f782180f8a7`
Source hash: `d4b8e5ff305e23e1e33f5592c078765e09c29dad154ad7cb2b42281ed705e4a5`

Family: bar. Relationship: cross-task comparison; repeated chart grammar. Usage: reference.

**Observed**

- The Asana result uses the same two-condition arrangement and pink/coral palette as the Slack figure.
- Direct values are 79.6 and 85.7 on the same zero-to-100 accuracy scale.

**Source-context paraphrase**: This is a separate held-out tool evaluation rather than the same Slack measurement repeated.

**Purpose**: Show that improvement size varies across task settings while preserving a fair visual comparison.

**Information mapping**: Within each figure, bars compare conditions; across the pair, task identity changes while axis, order and palette remain stable.

**Inferred rationale**: Keeping the scale fixed stops a smaller gain from looking equal to the larger gain in the companion figure. The two-figure group is analytically useful, not redundant decoration.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Generate both charts from a shared specification with task-specific data and captions. Use consistent ranges, condition order and direct labels; explain task differences outside the bar geometry.

**Reusable rules**

- Use shared scales across a comparison series.
- Preserve heterogeneous outcomes instead of presenting one universal gain.

**Counterexamples**

- Do not rescale each pair solely to maximize the apparent improvement.
- Do not average task scores without a justified weighting rule.

**Cross-domain transfer**

- Domain: Branch-service audits
- Mapping: Compare old and revised procedures separately at two service branches.
- Changes: Keep score definitions and axes identical, with separate sample descriptions.
- Limits: Different branch populations may explain part of the difference.

Original note: `runs/curation-02.json#E3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E4 — Detailed output makes information cost visible

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F5ed0d30526bf68624f335d075b8c1541be3bb595-1920x1006.png&w=3840)

Case `E4` · Figure `figure_3de5582e92265daab3a7ac28` · Article `article_1399aec680791f782180f8a7`
Source hash: `5e86cd526d63fdd787ec5c522596d941a9f6909a439a8c2f5e149a7b37a069bf`

Family: screenshot. Relationship: response anatomy; detailed fields; comparison baseline. Usage: reference.

**Observed**

- A dark monospaced console shows a detailed-format query and two multi-line records.
- Channel, sender, time, identifiers, text and attachment-related fields produce a visibly dense response.

**Source-context paraphrase**: The section discusses returning meaningful context and choosing tool response detail.

**Purpose**: Give readers a concrete baseline before a concise alternative; abstract advice about verbosity is insufficient without seeing what is retained or removed.

**Information mapping**: Aligned fields expose the response schema. Density reflects presented text, not a measured token budget. Some identifiers can be useful even when they increase length.

**Inferred rationale**: A realistic artifact allows the reader to assess information utility rather than equating shortness with quality. Pairing it with the concise example isolates the output contract.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Use a safe, original response with stable field order and accessible text. Highlight fields needed for follow-up operations, then compare with a carefully selected concise version.

**Reusable rules**

- Evaluate field utility before reducing response size.
- Preserve stable identifiers needed for subsequent actions.

**Counterexamples**

- Screenshot area is not token count.
- Detailed output is not automatically inferior.

**Cross-domain transfer**

- Domain: Inventory interfaces
- Mapping: Show a detailed item record containing location, supplier and audit identifiers.
- Changes: Mark operationally required fields and compare with a compact list view.
- Limits: A compact view must not remove information needed to identify or act on the item.

Original note: `runs/curation-02.json#E4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E5 — Concise output is a semantic selection, not smaller type

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fd4f649a66482efb5a80cf14ea85e84974ede1c49-1920x725.png&w=3840)

Case `E5` · Figure `figure_843a581d6152e524283555b0` · Article `article_1399aec680791f782180f8a7`
Source hash: `de97f3ee92247b048e2be66c4aef29d3a01e02ec752dfbe965f5edce805fb8c6`

Family: screenshot. Relationship: matched response; information reduction; drill-down. Usage: reference.

**Observed**

- The dark console retains the same general query and two-result structure but presents compact numbered rows.
- The response is shorter through selected content, not merely reduced font size or a tighter crop.

**Source-context paraphrase**: The article contrasts response formats while discussing useful context.

**Purpose**: Show which information can be omitted for the current task while preserving the ability to understand and use results.

**Information mapping**: Result identity stays stable as fields are reduced. The meaningful comparison is information retained versus removed, not the physical area of the two screenshots.

**Inferred rationale**: A matched query limits distracting changes and lets the reader inspect the actual contract difference. Concision is valuable when it preserves the next decision, not as an unconditional aesthetic preference.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Start from the detailed record and explicitly justify each omitted field. Keep enough identity for drill-down; provide accessible copyable output alongside the image.

**Reusable rules**

- Keep query and result identity fixed in format comparisons.
- Reduce irrelevant information rather than making all text smaller.

**Counterexamples**

- Do not infer token savings from image dimensions.
- Do not drop fields necessary for later actions merely to look clean.

**Cross-domain transfer**

- Domain: Archive search summaries
- Mapping: Show compact catalog hits linked to full records.
- Changes: Preserve title, date and stable record identifier while moving secondary fields to drill-down.
- Limits: A summary cannot be used as evidence of unseen full-record content.

Original note: `runs/curation-02.json#E5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E6 — An error can be longer and more useful

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F810661bd44a35fb273806ae95160040155978c3e-1920x850.png&w=3840)

Case `E6` · Figure `figure_7bfcd962220661973e185b42` · Article `article_1399aec680791f782180f8a7`
Source hash: `e5968de3b317f3d8200e1b7a60cc289b07d8f8e5ee46b7ad6099aad0389fc415`

Family: screenshot. Relationship: failure; diagnosis; repair guidance. Usage: reference.

**Observed**

- The console displays an invalid-identifier error with distinct sections for a summary, valid identifiers and resolution guidance.
- Sectioned monospaced text provides an actionable next step rather than only a machine code.

**Source-context paraphrase**: The section recommends informative tool errors that help an agent correct its next action.

**Purpose**: Demonstrate that response quality depends on recovery utility, not minimum length.

**Information mapping**: The failed value, allowable alternatives and repair action have separate textual roles. Headings support scanning while preserving exact identifiers.

**Inferred rationale**: Visible recovery guidance makes the failure productive; an opaque error would force guessing or repeated invalid calls. The example counters an overly simplistic always-be-concise rule.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Write an original error that names the failed field, states valid alternatives and offers a real supported recovery operation. Keep the API truth separate from user-facing explanation.

**Reusable rules**

- Include actionable repair information in expected failure responses.
- Optimize for the next correct action, not merely text length.

**Counterexamples**

- Do not invent a recovery command the tool cannot execute.
- Do not expose private account identifiers to make an example realistic.

**Cross-domain transfer**

- Domain: Application-form validation
- Mapping: Explain which field is invalid, show acceptable formats and provide a valid correction route.
- Changes: Use neutral sample values and actual form constraints.
- Limits: Guidance should not imply the corrected request will satisfy unrelated eligibility checks.

Original note: `runs/curation-02.json#E6`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E7 — A partial preview must identify the missing remainder

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fe440d6a69d0ca80e71f3bec5c2d00906ff03ce6d-1920x1162.png&w=3840)

Case `E7` · Figure `figure_60254d2af4a32cc623584f20` · Article `article_1399aec680791f782180f8a7`
Source hash: `ff1b64524feb5b293ec0e6ded36675a4f6b826ad714601be4814265b764e71af`

Family: screenshot. Relationship: truncation; total versus shown; pagination. Usage: reference.

**Observed**

- A dark response surface separates a scope/truncation notice from a small table preview.
- The result summary distinguishes the total result set from the displayed rows and includes filtering or continuation guidance.

**Source-context paraphrase**: The surrounding discussion addresses tool-response optimization and truncation.

**Purpose**: Help readers recognize a partial result without mistaking the preview for the complete evidence set.

**Information mapping**: The total count and visible-row count describe different populations. A continuation instruction identifies how to obtain missing evidence; it is not itself that evidence.

**Inferred rationale**: An explicit completeness boundary can prevent conclusions based only on conveniently visible rows. The notice belongs near the preview, not buried after a long output.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: State total, shown range and reason for truncation before the table. Supply an actual cursor or filtering action and keep the summary consistent with the full underlying query.

**Reusable rules**

- Distinguish total results from displayed results.
- Place completeness information adjacent to a preview.

**Counterexamples**

- Do not reconstruct unseen rows from a summary.
- A pagination hint does not prove subsequent pages were fetched.

**Cross-domain transfer**

- Domain: Stockroom search
- Mapping: Present the first few matching items with an explicit total and supported filters.
- Changes: Use real pagination state and preserve stable item identifiers.
- Limits: Do not draw inventory or purchasing conclusions from a partial preview alone.

Original note: `runs/curation-02.json#E7`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## E8 — A negative example isolates missing recovery information

Source: [Writing effective tools for agents — with agents](https://www.anthropic.com/engineering/writing-tools-for-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F2445187904704fec8c50af0b950e310ba743fac2-1920x733.png&w=3840)

Case `E8` · Figure `figure_f878dfa4779b281d2c020838` · Article `article_1399aec680791f782180f8a7`
Source hash: `d48fc55d2fbd4fe2ad67e7dba97926888dad2ebc6622cd0521a2565c3b04642c`

Family: screenshot. Relationship: opaque failure; contrast with actionable guidance. Usage: contrast_not_target.

**Observed**

- The same general invalid-request situation appears as nested JSON with code, status, field and value.
- The dark monospaced frame lacks the explanatory sections and recovery instructions visible in the companion error example.

**Source-context paraphrase**: The article discusses how error responses can guide correction rather than simply expose low-level failure details.

**Purpose**: Provide a specific counterexample so the positive recommendation is testable: the reader can identify exactly what recovery information is absent.

**Information mapping**: Nested syntax exposes a machine-oriented schema, while absent action guidance is the important difference. The format itself is not the failure mechanism.

**Inferred rationale**: Holding the request and console frame stable isolates actionability. Treating JSON as inherently bad would be a mistaken stylistic generalization.

**Palette**: Charts use restrained pink/coral against light neutral fields; console examples use near-black terminal surfaces with light text. These are distinct visual families within one article.

**Typography**: Charts use sans-serif labels and direct values; console examples use aligned monospaced text and visible section headings.

**Uncertain reconstruction**: Create a matched pair with identical failed input: one machine-only response and one response with valid alternatives and a supported next step. Label the former as a negative design case.

**Reusable rules**

- Include counterexamples in a style library, not only attractive successes.
- Compare missing semantic affordances rather than blaming a serialization format.

**Counterexamples**

- JSON may be appropriate for a wire protocol.
- Do not retrieve this negative example as an unqualified target style.

**Cross-domain transfer**

- Domain: Service API documentation
- Mapping: Contrast an opaque validation payload with a developer-facing repair explanation.
- Changes: Retain exact protocol fields while adding useful guidance in the appropriate layer.
- Limits: Readable wording cannot compensate for incorrect or unsupported recovery advice.

Original note: `runs/curation-02.json#E8`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F1 — A measurement metaphor is not measured evidence

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F7a472632580035058bc13bfea3b965fbb471a04e-1920x1079.png&w=3840)

Case `F1` · Figure `figure_cc30d05f87a64404d2e92aba` · Article `article_9bab7650498e417407496f1c`
Source hash: `88e4ed2645d2b71935549ec6da5eefc9629969b0ed168ba5dfe8b7371e7f5b45`

Family: cover. Relationship: human agency; measurement; editorial metaphor. Usage: reference.

**Observed**

- The cover has a lilac field, black hand-drawn hands and axes, and a white connected three-point motif.
- There is no numeric axis defining the white line as a data series. The actual background is lilac, despite conflicting color wording in source alternative text.

**Source-context paraphrase**: The article introduces an economic study of how people use Claude.

**Purpose**: Establish an editorial theme of people handling and interpreting measurements before quantitative findings begin.

**Information mapping**: Hands supply the human activity metaphor; axes and a line suggest measurement. Their arrangement is conceptual, and neither point count nor slope encodes an economic result.

**Inferred rationale**: A sparse hand-drawn cover can invite a general audience into a data-heavy article without pretending to be a scientific chart. Its expressive texture belongs to framing, not to the evidence figures that follow.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Choose an original measurement-related metaphor, draw a small number of expressive vector strokes or generated motifs and keep exact data out of the cover. Label it conceptual when a line could be mistaken for a real trend.

**Reusable rules**

- Separate editorial framing from evidence-bearing graphics.
- Verify visible properties rather than trusting inconsistent alt text.

**Counterexamples**

- Do not add a rising curve that implies unreported economic growth.
- Do not convert decorative points into invented observations.

**Cross-domain transfer**

- Domain: Community survey report
- Mapping: Use hands arranging neutral survey markers to introduce participatory measurement.
- Changes: Create new shapes and composition without Anthropic logos or copied artwork.
- Limits: The cover cannot communicate actual survey trends unless real data and axes are supplied.

Original note: `runs/curation-03.json#F1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F2 — Category cards align summaries without encoding area

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Feeed8fe5c58c7c956ddcf4a2c05d22204537897e-1968x1787.jpg&w=3840)

Case `F2` · Figure `figure_b99b69190dcc282b62fc6f27` · Article `article_9bab7650498e417407496f1c`
Source hash: `52f5ce56ff525df44484bd9ada11aba4b7451784a1ffc653765cffc16609524d`

Family: matrix. Relationship: taxonomy; examples; category share. Usage: reference.

**Observed**

- Six occupation cards form a three-column, two-row layout, with colored headers, percentage badges and pastel bodies.
- Each card separates leading titles from leading tasks; small icons and serif body text add an editorial character.

**Source-context paraphrase**: The figure describes occupational patterns in the sampled Claude conversations.

**Purpose**: Make a categorical summary concrete by connecting broad groups to recognizable titles and activities.

**Information mapping**: Header and color identify category; the badge reports share; repeated internal rows distinguish titles from tasks. Equal card area is a layout choice, not a treemap encoding.

**Inferred rationale**: A repeated card template permits scanning both across categories and within their examples. It reduces abstraction, but can encourage denominator confusion unless the caption identifies conversations rather than the entire workforce.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Prepare one schema for category, share, title examples and task examples. Keep row meanings fixed and label the denominator. Choose category colors without suggesting an ordinal ranking.

**Reusable rules**

- Keep separate taxonomies such as titles and tasks in separate rows.
- State the population behind percentage badges.

**Counterexamples**

- Card area does not show prevalence.
- Conversation share is not the percentage of workers in that occupation using AI.

**Cross-domain transfer**

- Domain: Library inquiry analysis
- Mapping: Summarize inquiry categories with shares, common patron questions and common requested actions.
- Changes: Use actual inquiry data and avoid mixing people, sessions and questions in one denominator.
- Limits: Example lists are illustrative, not a complete distribution within each category.

Original note: `runs/curation-03.json#F2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F3 — A privacy boundary plus a taxonomy-to-applications pipeline

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F174a18313abe71bd443d7c3681e7fb2e07a3e06a-2703x2032.jpg&w=3840)

Case `F3` · Figure `figure_1ce6409c4806d61f7c572be1` · Article `article_9bab7650498e417407496f1c`
Source hash: `b7c02662989c90a8b53e34d96a2961ea3fcddba16cba08357b9e2af0b7238f8d`

Family: flow. Relationship: private source; categorized tasks; public aggregate applications. Usage: reference.

**Observed**

- The upper sequence connects conversations, tasks and colored occupation groupings across three regions.
- An applications region below contains schematic scatter, donut and bar-like previews, visually separated from the transformation path.

**Source-context paraphrase**: The article explains a privacy-preserving analysis process for deriving aggregate insights from conversations.

**Purpose**: Explain how raw interactions become an interpretable taxonomy and then support multiple analyses, while distinguishing stages with different visibility.

**Information mapping**: The first row is a method pipeline; the lower charts are possible aggregate outputs. Privacy labeling describes a boundary, not a quantitative axis or a demonstrated guarantee.

**Inferred rationale**: Separating method from applications lets readers distinguish the data-processing story from the findings themselves. Small chart previews convey possible forms without needing full datasets at this stage.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Draw source access, transformation and aggregate outputs as explicit regions. Label what is private and what is shared, and mark mini-charts as schematic rather than measured findings.

**Reusable rules**

- Distinguish method diagrams from result diagrams.
- Keep access/privacy claims explicit and separate from decorative grouping.

**Counterexamples**

- A privacy label is not proof of anonymization or enforcement.
- Schematic chart previews must not be cited as quantitative evidence.

**Cross-domain transfer**

- Domain: Anonymous employee feedback
- Mapping: Convert protected responses into themes and then aggregate dashboards.
- Changes: Represent actual access controls and disclosure thresholds in prose, not merely a lock icon.
- Limits: Aggregation does not automatically eliminate reidentification risk.

Original note: `runs/curation-03.json#F3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F4 — Connected dots compare two populations, not two dates

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F189bf07829f2ecc1d3bac5961e9793a9e0f31e7d-3920x2948.jpg&w=3840)

Case `F4` · Figure `figure_ca32e0f21b6b3ba50882c25a` · Article `article_9bab7650498e417407496f1c`
Source hash: `41a127dee146b70a200360f472d4cab3a7a64f06c3ce578cf533b2fdbcabf1f2`

Family: dumbbell. Relationship: population share comparison; reference baseline; paired category. Usage: reference.

**Observed**

- Occupation rows connect coral Claude-conversation shares with dark workforce shares on one percentage axis.
- Thin horizontal segments join paired dots and direct values identify endpoints; there are no filled bars and no arrowheads. Source alt text calls this a bar chart, which conflicts with the pixels.

**Source-context paraphrase**: The figure compares occupational representation in Claude use with representation in the workforce.

**Purpose**: Make overrepresentation and underrepresentation easy to see without requiring readers to subtract two columns mentally.

**Information mapping**: Rows align the same occupation across different populations. Segment length expresses a difference between shares with different denominators; it is not a temporal change or individual adoption probability.

**Inferred rationale**: A dumbbell emphasizes the gap more efficiently than paired bars while retaining both baselines. The absence of arrows helps avoid implying a transition from one population into the other.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Align categories, place two directly labeled dots on a common scale and connect each pair neutrally. Name both denominator populations prominently and avoid temporal verbs.

**Reusable rules**

- Use connected dots when paired gaps, rather than totals, are the main question.
- Treat source alt-text mismatches as reviewable evidence discrepancies.

**Counterexamples**

- Do not describe the segment as growth over time.
- Do not equate the share gap with the percentage of workers adopting AI.

**Cross-domain transfer**

- Domain: Survey representativeness
- Mapping: Compare respondent-category shares with census shares.
- Changes: Use verified category definitions and explicitly identify each population and sampling period.
- Limits: A gap indicates composition differences, not their cause.

Original note: `runs/curation-03.json#F4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F5 — A scatterplot uses selective annotations and a reference line

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fe646b93b9bf29a8a7e164adb06e82251545f84d8-3936x3072.jpg&w=3840)

Case `F5` · Figure `figure_108bea18a528701dfefe6f66` · Article `article_9bab7650498e417407496f1c`
Source hash: `7f1c4461c499c0a48cf2326361fbbd3a25944d17676f00e0ad94d5a18b25e80f`

Family: scatter. Relationship: bivariate distribution; outliers; reference threshold. Usage: reference.

**Observed**

- Coral points form a cloud, with a small set of points labeled in bold.
- A vertical dotted reference marks median wage, while axes describe wage and conversation share. No fitted trend line is visible.

**Source-context paraphrase**: The article examines the relationship between occupational wages and observed Claude-use patterns.

**Purpose**: Expose the shape and exceptions of a bivariate relationship that a single average could obscure.

**Information mapping**: Each point represents an occupational observation; x and y encode different quantities; the vertical line supplies a reference, not an estimated causal effect.

**Inferred rationale**: Selective labeling preserves legibility in a dense cloud. The reference line provides orientation without asserting a regression, although choosing only striking labels can bias attention.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Use the actual two-variable table, define the point unit, document an annotation rule and draw only supported reference statistics. Provide a searchable data table for unlabeled points.

**Reusable rules**

- Define the observational unit of every point.
- Separate reference thresholds from fitted relationships.

**Counterexamples**

- Usage share is not the probability that an individual at a given wage uses AI.
- An association is not a causal wage effect.

**Cross-domain transfer**

- Domain: Museum exhibit planning
- Mapping: Plot actual exhibit costs against observed visitor interest, with a documented budget reference.
- Changes: Use a consistent observation period and a rule for labeling outliers.
- Limits: Popularity and cost may share confounders; the scatter does not establish causation.

Original note: `runs/curation-03.json#F5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## F6 — Unequal stacked totals preserve the global denominator

Source: [The Anthropic Economic Index](https://www.anthropic.com/research/the-anthropic-economic-index) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F1f17c9364b316df226154831452a50b53431817a-2200x1250.jpg&w=3840)

Case `F6` · Figure `figure_d801601c482104b1c6b4a68d` · Article `article_9bab7650498e417407496f1c`
Source hash: `e74b9865ed7de3e506b8a7b6f30af065a6446a7ded925adf753efe8934e3f6f5`

Family: stacked_bar. Relationship: part-to-whole; mode groups; common denominator. Usage: reference.

**Observed**

- Two horizontal stacks use blue segments for one mode and purple segments for another, with legends to the right.
- The top and bottom totals are approximately 57.4 and 42.6 rather than two independently normalized 100-percent bars.

**Source-context paraphrase**: The article distinguishes augmentation and automation-related interaction patterns.

**Purpose**: Show both the balance between broad modes and the composition of their subcategories without silently changing the denominator.

**Information mapping**: Each segment contributes to a shared conversation population. Stack lengths carry group share; color identifies subcategories. The two rows do not each represent a complete population.

**Inferred rationale**: Unequal row lengths preserve the between-mode comparison while subdivision retains useful detail. Renormalizing each row would answer a different question and could conceal the main balance.

**Palette**: The series mixes lilac conceptual artwork, pastel category cards, coral/dark comparison marks and blue/purple categorical stacks. It is not one uniform chart palette.

**Typography**: Data figures combine clear labels and annotations; category cards have visibly serif body text. The cover uses very little text. Exact font families were not identified.

**Uncertain reconstruction**: Build a table of counts and one common denominator. Verify that segment sums and group totals agree, label both and state whether the categories exhaust the population.

**Reusable rules**

- Keep a shared denominator visible in grouped composition charts.
- Validate segment and group sums before rendering.

**Counterexamples**

- Do not normalize each row to 100 percent without changing the explanation.
- Interaction mode is not a measured fraction of jobs replaced.

**Cross-domain transfer**

- Domain: Service-ticket handling
- Mapping: Show assisted versus automated tickets, subdivided into actual handling patterns.
- Changes: Use ticket counts from one defined period and mutually interpretable categories.
- Limits: Do not turn interaction classification into a labor-displacement claim.

Original note: `runs/curation-03.json#F6`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## G1 — File anatomy separates metadata from instructions

Source: [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F6f22d8913dbc6228e7f11a41e0b3c124d817b6d2-1650x929.jpg&w=3840)

Case `G1` · Figure `figure_1fa668dc739389d3da0405da` · Article `article_2e50ab69ebea3b2c9412dd48`
Source hash: `d05c97b366af2c73a1bdf52c42201ecba89796b70e9c6bc762eb5d1c88579053`

Family: anatomy. Relationship: one artifact; two information levels. Usage: reference.

**Observed**

- A central blue file card has a darker header-like metadata region and a lighter body region.
- External labels identify the file and its parts; code-like text makes the boundary between metadata and Markdown visible.

**Source-context paraphrase**: The article introduces a Skill file with metadata and instructions.

**Purpose**: Teach the artifact's structure before describing how an agent discovers and loads it.

**Information mapping**: Both colored regions belong to one file. Tone distinguishes information roles, not separate services, and the filename provides a concrete anchor for the abstraction.

**Inferred rationale**: An annotated specimen makes a file format easier to understand than prose alone while avoiding an overwhelming complete file listing.

**Palette**: Warm neutral canvas; blue primary files, gold references and violet executable material; restrained green/orange context annotations.

**Typography**: Monospaced file names and code-like content combined with short sans-serif callouts. Exact typeface not established.

**Uncertain reconstruction**: Use a short original example, split its regions according to the real format and place callouts outside the specimen. Provide complete accessible text rather than relying on a screenshot of code.

**Reusable rules**

- Use an annotated specimen when structure within one artifact is the question.
- Keep metadata and executable or instructional body semantics distinct.

**Counterexamples**

- Do not imply that the two regions are separate files.
- A specimen image is not a validated implementation.

**Cross-domain transfer**

- Domain: Policy handbooks
- Mapping: Show a policy's summary metadata above its operational procedure.
- Changes: Use actual policy fields and an original compact example.
- Limits: Visual anatomy does not establish that the policy is complete or compliant.

Original note: `runs/curation-03.json#G1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## G2 — Reference links point to exact supporting artifacts

Source: [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F191bf5dd4b6f8cfe6f1ebafe6243dd1641ed231c-1650x1069.jpg&w=3840)

Case `G2` · Figure `figure_1615bbbbd3fb4085c7fb93ed` · Article `article_2e50ab69ebea3b2c9412dd48`
Source hash: `ee139a4cad293dd8fc85c52696058e8797de6ba73dada61fe60ff3948091c109`

Family: anatomy. Relationship: indirection; linked references; optional detail. Usage: reference.

**Observed**

- A tall blue primary file sits on the left, with gold reference and forms cards stacked on the right.
- Highlighted inline paths connect through orthogonal gold lines to specific target cards.

**Source-context paraphrase**: The article explains bundling supporting files and referencing them from Skill instructions.

**Purpose**: Show how a compact entry point can expose deeper material without containing all of it.

**Information mapping**: Highlighted text is a reference location; the target card is a separate artifact. Connectors mean links, not execution order or proof that all linked files were loaded.

**Inferred rationale**: Connecting exact source text to exact target cards reduces ambiguity about indirection and helps readers understand progressive access.

**Palette**: Warm neutral canvas; blue primary files, gold references and violet executable material; restrained green/orange context annotations.

**Typography**: Monospaced file names and code-like content combined with short sans-serif callouts. Exact typeface not established.

**Uncertain reconstruction**: Draw a short primary document, highlight only real reference locations and route each to its own supporting artifact. Label the relationship as a reference and preserve distinguishable filenames.

**Reusable rules**

- Connect the exact reference location to its target.
- Distinguish linking a file from loading or executing it.

**Counterexamples**

- Do not imply every reference is always present in context.
- Do not turn links into a required serial workflow.

**Cross-domain transfer**

- Domain: Employee onboarding
- Mapping: A short handbook points to benefits details and application forms.
- Changes: Use real file or section names and show on-demand detail without cluttering the entry guide.
- Limits: The diagram cannot prove that a new employee read the linked material.

Original note: `runs/curation-03.json#G2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## G3 — A table is the right diagram for a loading policy

Source: [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fa3bca2763d7892982a59c28aa4df7993aaae55ae-2292x673.jpg&w=3840)

Case `G3` · Figure `figure_28f1275ed4c5793fa807a3b2` · Article `article_2e50ab69ebea3b2c9412dd48`
Source hash: `ff8ce15c9de587f0bd00ed8de4ffdd08efbec747d908f3401e27ac9e17941099`

Family: matrix. Relationship: levels; file classes; loading conditions; size guidance. Usage: reference.

**Observed**

- A four-column table organizes levels, files, context-window conditions and token guidance across three rows.
- Thin rules and gray cells dominate; a starred unlimited entry depends on explanatory context rather than a literal infinite model window.

**Source-context paraphrase**: The article describes progressive disclosure of Skill metadata, instructions and supporting material.

**Purpose**: Make several independent properties of each level comparable. Arrows would add chronology where a policy table is the actual information structure.

**Information mapping**: Rows identify levels and columns identify attributes. Approximate size guidance and loading conditions are different fields, and the footnote qualifies the apparent unlimited category.

**Inferred rationale**: A restrained table permits precise cross-reading without decorative complexity. Preserving qualifications is essential because an isolated unlimited label is easy to misread.

**Palette**: Warm neutral canvas; blue primary files, gold references and violet executable material; restrained green/orange context annotations.

**Typography**: Monospaced file names and code-like content combined with short sans-serif callouts. Exact typeface not established.

**Uncertain reconstruction**: Keep one row per actual level, separate loading conditions from size guidance and place qualifications next to the relevant cell. Use text wrapping rather than shrinking footnotes below readable size.

**Reusable rules**

- Choose tables for multi-attribute policy comparisons.
- Preserve footnotes that change the meaning of a cell.

**Counterexamples**

- Do not interpret external material capacity as unlimited model context.
- Do not fabricate token budgets for a new domain.

**Cross-domain transfer**

- Domain: Documentation navigation
- Mapping: Compare catalog summaries, on-demand instructions and external appendices.
- Changes: Replace token guidance with real reading or storage constraints only when known.
- Limits: A policy table does not demonstrate runtime enforcement.

Original note: `runs/curation-03.json#G3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## G4 — Show what is loaded at the decision point

Source: [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F441b9f6cc0d2337913c1f41b05357f16f51f702e-1650x929.jpg&w=3840)

Case `G4` · Figure `figure_6b1767239b8b1096126cb2d4` · Article `article_2e50ab69ebea3b2c9412dd48`
Source hash: `904182c03e4677d45d24fe972c382e90e6de35b9a69d1fe0912912b2a90ce325`

Family: sequence. Relationship: progressive disclosure; context boundary; triggered reads. Usage: reference.

**Observed**

- A context-window container begins with blue metadata chips, followed by staggered conversation and tool-related elements.
- Orange actions and blue results are interleaved; green external callouts point to the moments that trigger further file reads.

**Source-context paraphrase**: The article explains loading deeper Skill content when the task requires it.

**Purpose**: Resolve the difference between a file existing on disk and its content being available to the model now.

**Information mapping**: Containment means loaded context, order means the interaction sequence, and callouts explain the trigger for additional detail. External files are not automatically inside the window.

**Inferred rationale**: A concrete timeline of reads makes progressive disclosure operational rather than merely a file-tree organization principle.

**Palette**: Warm neutral canvas; blue primary files, gold references and violet executable material; restrained green/orange context annotations.

**Typography**: Monospaced file names and code-like content combined with short sans-serif callouts. Exact typeface not established.

**Uncertain reconstruction**: Enumerate always-present metadata, user input, triggered reads and returned content. Draw the context boundary around actual loaded material and attach callouts to the relevant actions.

**Reusable rules**

- Keep stored and loaded information distinct.
- Explain the trigger for a new disclosure level.

**Counterexamples**

- File presence is not evidence that its contents were read.
- A context container must not silently include every linked appendix.

**Cross-domain transfer**

- Domain: Caseworker guidance
- Mapping: A worker starts with a brief, selects a relevant policy and then opens the necessary appendix.
- Changes: Show actual decision triggers and preserve the difference between catalog availability and consulted guidance.
- Limits: The sequence is not evidence of compliance or complete reading.

Original note: `runs/curation-03.json#G4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## G5 — Instructions can reference executable support without containing it

Source: [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fc24b4a2ff77277c430f2c9ef1541101766ae5714-1650x929.jpg&w=3840)

Case `G5` · Figure `figure_6cf54dd94e901d466567b404` · Article `article_2e50ab69ebea3b2c9412dd48`
Source hash: `cee4f6e5f2c3d572a8381e4da276e2684bc1925db49fb7668106676e75cf23a5`

Family: anatomy. Relationship: instruction-to-script reference; separate artifact roles. Usage: reference.

**Observed**

- A gold forms/instructions card sits beside a violet Python-script card.
- A highlighted script path connects orthogonally to the executable file, with monospaced filenames above the content.

**Source-context paraphrase**: The article describes including code that agents can execute as part of a Skill.

**Purpose**: Explain how reusable computation complements instructions, while keeping the two artifacts conceptually distinct.

**Information mapping**: The instruction tells when or how to use the script; the target contains implementation. The line is a reference, not proof of a completed process or output.

**Inferred rationale**: Different artifact colors and exact path highlighting make delegation to code understandable without requiring the whole program in the illustration.

**Palette**: Warm neutral canvas; blue primary files, gold references and violet executable material; restrained green/orange context annotations.

**Typography**: Monospaced file names and code-like content combined with short sans-serif callouts. Exact typeface not established.

**Uncertain reconstruction**: Use an original brief instruction and a compact code specimen, label execution prerequisites and keep source text editable. Show an output only when it was actually produced or explicitly labeled hypothetical.

**Reusable rules**

- Distinguish instructional and executable artifacts.
- Do not confuse a reference edge with execution evidence.

**Counterexamples**

- A screenshot of code does not prove it ran.
- Do not copy an entire implementation into an unreadable diagram.

**Cross-domain transfer**

- Domain: Workshop procedures
- Mapping: A manual references a calculation worksheet used for a specific measurement step.
- Changes: Identify when the worksheet is needed and which values it accepts.
- Limits: The reference cannot guarantee valid calculations or correct operator input.

Original note: `runs/curation-03.json#G5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## H1 — Matched experimental conditions with a prominent scope qualification

Source: [Alignment faking in large language models](https://www.anthropic.com/research/alignment-faking) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fc704ae324f51c73c9a723aed7f725d6a28159380-2200x1690.png&w=3840)

Case `H1` · Figure `figure_bf0c91fb104641b2970bde52` · Article `article_c2cfd72baafd64a9812693a4`
Source hash: `dd927cf5f36f10e2b679e0b2ec2b0380dcb36eb96cc85a1f07ea64763b1d2ff8`

Family: comparison. Relationship: controlled condition; observed response; fictional setup. Usage: reference.

**Observed**

- A shared instruction banner branches into two equal panels for different account conditions, with matching request positions.
- Dashed internal-reasoning specimens, response regions and condition summaries repeat across the pair. A footnote qualifies the scenario as an experimental setup rather than an actual user-data policy.

**Source-context paraphrase**: The article studies behavior under an experimental training-versus-unmonitored distinction.

**Purpose**: Make the controlled difference visible while preventing the experimental scenario from being mistaken for a description of deployed policy.

**Information mapping**: Shared banner and repeated positions identify held-constant material; panel identity identifies the changed condition. Specimens illustrate behavior in the experiment and do not establish universal intent.

**Inferred rationale**: A matched layout lowers comparison cost, but omitted footnotes could radically change the reader's interpretation. Qualifications belong in the reusable structure, not optional decoration.

**Palette**: Matched pale gray/white experimental panels with pink emphasis; outcome figures use cyan, yellow and a red hatch as distinct categories.

**Typography**: Readable condition headings, compact response specimens and small but meaning-bearing footnotes; chart labels share a clear hierarchy.

**Uncertain reconstruction**: Use neutral original examples, hold the request constant and label the condition difference explicitly. Preserve the experiment-only qualification and avoid reproducing harmful example responses.

**Reusable rules**

- Hold shared inputs visually constant in condition comparisons.
- Keep experiment-versus-deployment qualifications attached to the graphic.

**Counterexamples**

- Do not describe a fictional experimental policy as the actual service policy.
- A selected example does not establish population-wide behavior.

**Cross-domain transfer**

- Domain: Review-bias experiment
- Mapping: Compare anonymous and identified review conditions using the same neutral submission.
- Changes: Label the manipulated condition and distinguish illustrative responses from aggregate outcomes.
- Limits: Matched visual layout does not by itself prove causal identification.

Original note: `runs/curation-03.json#H1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## H2 — Condition snapshots and a training trajectory answer different questions

Source: [Alignment faking in large language models](https://www.anthropic.com/research/alignment-faking) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F6cc34bfc6ba9f1e033a62fcde68ed97b88c9a253-2200x1152.png&w=3840)

Case `H2` · Figure `figure_78abede8e2b81a200b13c33c` · Article `article_c2cfd72baafd64a9812693a4`
Source hash: `797ec6ac06e7a0f2130aed29c8bef9500250bc82ddfb559ebc8bae0d5cf09be4`

Family: small_multiples. Relationship: outcome composition; training stages; uncertainty of interpretation. Usage: reference.

**Observed**

- Three panels combine paired stacked bars with a stacked-area training trajectory and a separate final bar.
- Cyan, yellow and red-hatched regions distinguish refusal and different compliance-related categories; the hatch supplies a non-color cue. Fraction scales and internal condition labels organize the panels.

**Source-context paraphrase**: The article compares prompted and trained settings and reports behavior over training.

**Purpose**: Separate initial condition differences from how outcome composition changes during training, without collapsing all behavior into one success score.

**Information mapping**: Stack segments identify outcome categories, panel identity distinguishes experimental settings, and horizontal position in the trajectory encodes training progress. Category definitions must remain tied to the original legend.

**Inferred rationale**: Using both snapshots and a trajectory lets readers see endpoints and development. Patterning makes the highlighted behavioral category more accessible, but density requires careful legend placement.

**Palette**: Matched pale gray/white experimental panels with pink emphasis; outcome figures use cyan, yellow and a red hatch as distinct categories.

**Typography**: Readable condition headings, compact response specimens and small but meaning-bearing footnotes; chart labels share a clear hierarchy.

**Uncertain reconstruction**: Define mutually interpretable categories first, verify stack sums and retain shared fraction scales. Label snapshots separately from training steps and reproduce category semantics exactly rather than relying only on color names.

**Reusable rules**

- Use separate panels for condition comparisons and temporal trajectories.
- Add a pattern or label when a critical category must not depend on color alone.

**Counterexamples**

- Do not turn these experimental frequencies into deployment incident rates.
- Do not merge distinct compliance categories merely to simplify the palette.

**Cross-domain transfer**

- Domain: Procedure-training evaluation
- Mapping: Show the proportions of several neutral decision outcomes before, during and after training.
- Changes: Use actual observations and explicit category definitions with an accessible data table.
- Limits: Training-step changes may not generalize to a different setting or participant population.

Original note: `runs/curation-03.json#H2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## J1 — Selective inquiry turns a large codebase into a bounded explanation

Source: [How the Claude Code team does code review](https://claude.dev/blog/solving-code-review-at-anthropic/) · [Inspected occurrence](https://claude.dev/media/d4fafcbf3d2137bc64eb7f0707ba91809ff1ed4d098785e36d7422b3b07f6c40.png)

Case `J1` · Figure `figure_2d212d011d7d56a38c7fb695` · Article `article_c7dfeeeb6dfb1916550c27a8`
Source hash: `bb5c71f6104b104de42efc22386d250e28bd9bc9fea7c68cbcfe82b06ae27d32`

Family: concept. Relationship: focus; selective explanation; optional artifact. Usage: reference.

**Observed**

- A grid representing a codebase highlights a few salmon-colored cells among gray cells.
- A question connects through a solid hand-drawn arrow to an orange-accented explanation card, while a dashed branch leads to a secondary artifact card. Contrasting summary bands sit below.

**Source-context paraphrase**: The article discusses learning about code through targeted explanations rather than treating review as reading every trivial change.

**Purpose**: Make the scope of a useful inquiry visible: the reader needs a specific explanation, not an undifferentiated view of the entire codebase.

**Information mapping**: The grid is a conceptual collection, highlighted cells identify a selected concern, and the two output paths distinguish an explanation from an optional supporting artifact. Cell counts do not measure code coverage.

**Inferred rationale**: Selective color concentrates attention before the explanatory handoff. A dashed secondary path can make optional depth visible without distracting from the primary answer, provided optionality is labeled.

**Palette**: Warm neutral ground, dark high-contrast text, orange editorial emphasis and a restrained blue secondary artifact cue.

**Typography**: Large question-like heading, small eyebrow label, short explanatory cards and contrasting summary bands; exact font unknown.

**Uncertain reconstruction**: Create an original collection grid, highlight only the relevant subset and write a concrete reader question. Route one primary answer and one explicitly optional detail artifact. Keep the main takeaway in a short, high-contrast band.

**Reusable rules**

- Show the selected scope before expanding it.
- Distinguish a primary answer from optional depth.

**Counterexamples**

- Grid-cell count is not measured coverage or comprehension.
- Do not interpret a dashed optional branch as a failure path.

**Cross-domain transfer**

- Domain: City planning guidance
- Mapping: Highlight a district on a conceptual map and connect a resident's question to a permit explanation and optional worked example.
- Changes: Use actual jurisdictional scope and a new composition rather than copying code-specific motifs.
- Limits: A selective explanation is not a complete review of all applicable rules.

Original note: `runs/curation-04.json#J1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## BL1 — A pipeline overview expands into a bounded interaction loop

Source: [Bloom: an open source tool for automated behavioral evaluations](https://alignment.anthropic.com/2025/bloom-auto-evals/) · [Inspected occurrence](https://alignment.anthropic.com/2025/bloom-auto-evals/fig2.png)

Case `BL1` · Figure `figure_473e0fb0e6d005e90bec75e0` · Article `article_7fa7d4cb797a5edd5aa247f3`
Source hash: `7ceaaf37b6036d9b432afc1e0202442c2e18da995552f31f95295ed78cc6cf14`

Family: flow. Relationship: overview-detail; generation; rollout; judgment. Usage: reference.

**Observed**

- A small input-pipeline-transcript overview is linked to a larger four-stage view of understanding, ideation, rollout and judgment.
- Configuration labels sit near their owning stages. The rollout includes repeated turns and stopping choices; judge sliders appear as schematic outputs.

**Source-context paraphrase**: Bloom generates evaluation scenarios, runs interactions and judges the resulting behavior.

**Purpose**: Explain both the overall evaluation lifecycle and the internal work hidden inside the pipeline label.

**Information mapping**: Stage containers identify responsibility; configuration labels identify controls; the loop expresses interaction rather than a one-shot call. Slider marks are illustrative, not measured benchmark results.

**Inferred rationale**: A linked overview/detail layout lets readers orient first and inspect a complex stage second. Keeping stopping conditions inside the rollout avoids suggesting unlimited interaction.

**Palette**: Pale stage containers, green configuration labels, pink agent roles and gold target-model roles; categorical accents rather than quantitative color scales.

**Typography**: Short stage headings, compact parameter labels and callouts; exact font unknown.

**Uncertain reconstruction**: Write the high-level pipeline, choose the stage requiring detail and expand it with explicit inputs, repeated actions and stopping rules. Associate parameters with their owning stage and label output previews schematic.

**Reusable rules**

- Use a linked overview and expansion rather than one overloaded diagram.
- Make iteration controls and stopping choices part of the explanation.

**Counterexamples**

- Do not cite decorative slider positions as experimental outcomes.
- Do not present every possible rollout action as mandatory.

**Cross-domain transfer**

- Domain: Training-course assessment
- Mapping: Define a rubric, generate exam scenarios, administer bounded interactions and score individual items and the overall suite.
- Changes: Use actual assessment controls and separate per-item scores from suite-level interpretation.
- Limits: Automated judgment does not establish the validity of the assessment itself.

Original note: `runs/curation-04.json#BL1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## V1 — A claimed result is not an environmental outcome

Source: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fbd42e7b2f3e9bb5218142796d3ede4816588dec0-4584x2834.png&w=3840)

Case `V1` · Figure `figure_8ea89b522837bc08ca27b566` · Article `article_6c790a16de0afd2b5fe6beb9`
Source hash: `5cc2219c08305f7a6e2049415ebc0658092ef9ac2ca3bce7dcc890a0bf558ffa`

Family: comparison. Relationship: single response versus stateful task; outcome verification. Usage: reference.

**Observed**

- The upper panel moves prompt/data through a model and response to grading.
- The lower panel adds tools, environment changes and an agent trace, with grading tied to task outcomes. A coral callout emphasizes the distinction between saying something was done and actually doing it.

**Source-context paraphrase**: The article distinguishes agent evaluation from evaluating an isolated response and discusses outcomes and graders.

**Purpose**: Prevent readers from treating persuasive completion text as proof of successful action.

**Information mapping**: The repeated grading destination supports comparison, but the evidence feeding it changes: response text in one case, trace and environment outcome in the other.

**Inferred rationale**: A matched pair makes the extra evaluation burden concrete without requiring a long definition. The callout gives the reader a memorable failure mode to check in new systems.

**Palette**: Warm/light neutral panels, mint or green actors, coral outcome emphasis and violet maintenance categories; metaphorical layers use ochre and cream.

**Typography**: Large panel labels, compact role names, direct annotations and distinct captions; exact fonts and numeric color values unmeasured.

**Uncertain reconstruction**: Choose one task with an externally inspectable outcome, draw the response path and actual-state path separately and attach grading to the proper evidence. Keep a completion claim visibly distinct from the state it describes.

**Reusable rules**

- Grade the outcome a task actually requires.
- Separate reported completion from independently observable state.

**Counterexamples**

- Do not use response-only grading for tasks that require external changes.
- An environment box does not prove an evaluator inspected the environment.

**Cross-domain transfer**

- Domain: Warehouse dispatch
- Mapping: Contrast an operator's delivery confirmation with the inventory reservation and carrier handoff records.
- Changes: Define actual success criteria and trace each check to its source.
- Limits: A polished confirmation message is not evidence that the parcel moved.

Original note: `runs/curation-04.json#V1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## V2 — Nested containment makes evaluation vocabulary precise

Source: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F0205b36f9639fc27f2f6566f73cb56b06f59d555-4584x2580.png&w=3840)

Case `V2` · Figure `figure_3d8721ac59b06e22f78c19e8` · Article `article_6c790a16de0afd2b5fe6beb9`
Source hash: `66f4062493c52ba519ccdc3334e2e254e098cf215c8796887941c6b8959cb6c2`

Family: hierarchy. Relationship: harness; suite; task; trial; grader; metric. Usage: reference.

**Observed**

- Nested rectangles organize an evaluation harness, suite and tasks, with stacked trial cards and distinct grader/metric chips.
- An external agent-harness exchange and a separate outcome-to-grader path prevent all concepts from becoming one nested list.

**Source-context paraphrase**: The article defines tasks, trials, graders, outcomes and evaluation harnesses.

**Purpose**: Resolve vocabulary that otherwise sounds interchangeable, particularly the difference between a task definition and repeated attempts.

**Information mapping**: Containment expresses ownership or grouping, not chronology. Trial stacks represent repetitions; grader and metric chips represent assessment functions and recorded values, not additional attempts.

**Inferred rationale**: A hierarchy reduces the number of definitions readers must hold in memory. Extra cross-boundary edges show relationships that containment alone cannot express.

**Palette**: Warm/light neutral panels, mint or green actors, coral outcome emphasis and violet maintenance categories; metaphorical layers use ochre and cream.

**Typography**: Large panel labels, compact role names, direct annotations and distinct captions; exact fonts and numeric color values unmeasured.

**Uncertain reconstruction**: Define the nouns and their ownership relations first. Use nesting only for genuine containment and labeled edges for execution or grading. Include a compact legend that distinguishes record types.

**Reusable rules**

- Do not use containment to imply time order.
- Keep definitions, attempts, assessments and measurements distinct.

**Counterexamples**

- A task is not a trial count.
- A recorded metric is not automatically a correct assessment.

**Cross-domain transfer**

- Domain: Factory quality campaigns
- Mapping: A campaign contains procedures, each procedure is repeated in trials and its outcomes are assessed by checks that record metrics.
- Changes: Use actual ownership boundaries and preserve the distinction between test design and execution records.
- Limits: The taxonomy does not prove adequate coverage or measurement validity.

Original note: `runs/curation-04.json#V2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## V3 — At least one success and consistent success diverge

Source: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F3ddac5be07a0773922ec9df06afec55922f8194a-4584x2580.png&w=3840)

Case `V3` · Figure `figure_f5487c8f6e69ad3cf559e3b9` · Article `article_6c790a16de0afd2b5fe6beb9`
Source hash: `9a35813ba60ca258226524796c94b99a05dda5fdc30f30dc715683bf0a5f30c0`

Family: line. Relationship: probability definitions; repeated trials; opposite trends. Usage: reference.

**Observed**

- Mint and coral curves share a zero-to-100-percent success scale and trial counts from one to ten.
- The curves start together and diverge upward and downward, with a dashed reference and large endpoint callouts.

**Source-context paraphrase**: The section distinguishes pass-at-k from success across repeated trials.

**Purpose**: Expose why more attempts can improve the chance of one success while making consistent success across all attempts harder.

**Information mapping**: The x-axis counts attempts and each curve encodes a different event definition. The curves are an explanatory probability illustration, not a production performance trace.

**Inferred rationale**: Opposite directions on a common scale make the definitional distinction difficult to miss. A familiar rising curve alone could wrongly encourage using one metric for every reliability question.

**Palette**: Warm/light neutral panels, mint or green actors, coral outcome emphasis and violet maintenance categories; metaphorical layers use ochre and cream.

**Typography**: Large panel labels, compact role names, direct annotations and distinct captions; exact fonts and numeric color values unmeasured.

**Uncertain reconstruction**: State the event definitions and assumptions before plotting. For constant independent trial success probability p, compute 1-(1-p)^k and p^k; label that model explicitly and do not imply independence when trials are correlated.

**Reusable rules**

- Choose a metric according to the event the reader cares about.
- Label model assumptions on conceptual probability curves.

**Counterexamples**

- Do not treat these curves as measured deployment outcomes.
- Independent-trial formulas need not describe correlated retries.

**Cross-domain transfer**

- Domain: Search versus dependable service
- Mapping: Contrast finding one usable candidate after repeated searches with a service succeeding on every request.
- Changes: Use a justified probability model or actual observed trials, and name the event precisely.
- Limits: A high at-least-one success rate cannot certify consistent service reliability.

Original note: `runs/curation-04.json#V3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## V4 — A phased roadmap is not a schedule

Source: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F0db40cc0e14402222a179fc6297b9c8818e97c8a-4584x2580.png&w=3840)

Case `V4` · Figure `figure_6598d72dc2d0c97d8002e09f` · Article `article_6c790a16de0afd2b5fe6beb9`
Source hash: `4f9cbf1ec24a45068de10a61d1916590e4c955ddfa2e90ae0eac2f333c186d60`

Family: flow. Relationship: phases; ordered actions; maintenance. Usage: reference.

**Observed**

- Three colored phase columns group numbered actions: green suite work, coral harness work and violet maintenance.
- A continuous orthogonal path connects unequal numbers of actions in each phase; the columns have different occupied heights.

**Source-context paraphrase**: The article offers a progression for building and maintaining evaluations.

**Purpose**: Help readers locate the next type of work without confusing suite design, execution machinery and ongoing maintenance.

**Information mapping**: Color identifies phase, numbers express order and connectors express the proposed progression. Neither column width nor action spacing is a duration scale.

**Inferred rationale**: Phase grouping can reduce a long list into memorable responsibilities. Numbering preserves local order while the colored columns provide a broader mental map.

**Palette**: Warm/light neutral panels, mint or green actors, coral outcome emphasis and violet maintenance categories; metaphorical layers use ochre and cream.

**Typography**: Large panel labels, compact role names, direct annotations and distinct captions; exact fonts and numeric color values unmeasured.

**Uncertain reconstruction**: Group actual actions by responsibility, number their logical order and route a readable connecting path. Keep maintenance visibly ongoing when that is the real process; avoid dates unless there is a schedule.

**Reusable rules**

- Separate logical order from elapsed time.
- Group related actions without hiding their individual sequence.

**Counterexamples**

- Do not read phase height as effort or duration.
- Maintenance is not necessarily a final completed state.

**Cross-domain transfer**

- Domain: Editorial quality program
- Mapping: Define a review corpus, establish review procedures and maintain checks as content changes.
- Changes: Use the team's actual responsibilities and ongoing feedback path.
- Limits: A roadmap does not guarantee resourcing or successful completion.

Original note: `runs/curation-04.json#V4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## V5 — Layered checks have residual gaps

Source: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fb77b8dbb7c2e57f063fbc8a087a853d5809b74b0-4584x2580.png&w=3840)

Case `V5` · Figure `figure_5760238d9b420e0702413d10` · Article `article_6c790a16de0afd2b5fe6beb9`
Source hash: `7eb74934c6f75813e7f3f245919ca6d1a3dd839e709206a59e10ceb20f66f64e`

Family: concept. Relationship: defense in depth; partial coverage; residual risk. Usage: reference.

**Observed**

- Three offset ochre/cream layers contain circular gaps, with red failure paths passing through or being stopped.
- Cross marks and leader labels distinguish layers; some paths continue beyond the layered structure.

**Source-context paraphrase**: The article discusses complementary evaluation approaches rather than one perfect assessment method.

**Purpose**: Communicate that different checks can catch different failures while retaining the possibility of misses.

**Information mapping**: Layers stand for assessment approaches, holes stand for limitations and paths stand for possible failure trajectories. Hole area and path count are metaphorical, not calibrated probabilities.

**Inferred rationale**: Visible residual paths resist the misleading message that stacking checks eliminates risk. Offsetting gaps makes complementarity understandable without a complex statistical model.

**Palette**: Warm/light neutral panels, mint or green actors, coral outcome emphasis and violet maintenance categories; metaphorical layers use ochre and cream.

**Typography**: Large panel labels, compact role names, direct annotations and distinct captions; exact fonts and numeric color values unmeasured.

**Uncertain reconstruction**: Create original layered geometry, label each real check and leave an explicitly labeled residual path. State that the metaphor is not quantitative; do not scale holes to invented failure rates.

**Reusable rules**

- Show residual risk when illustrating layered checks.
- Keep metaphorical coverage distinct from measured effectiveness.

**Counterexamples**

- More layers do not prove independent coverage.
- Decorative hole area is not an estimated error probability.

**Cross-domain transfer**

- Domain: Publishing review
- Mapping: Fact review, style review and legal review have different limitations and may still miss a problem.
- Changes: Use actual review responsibilities and explicitly label remaining uncertainty.
- Limits: The layers do not establish independence or guarantee a safe publication.

Original note: `runs/curation-04.json#V5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## T1 — Multiple surface forms can share an intermediate structure

Source: [Tracing the thoughts of a large language model](https://www.anthropic.com/research/tracing-thoughts-language-model) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fe0e156ea6c912a385d66ed562187fced8c392a58-1650x750.png&w=3840)

Case `T1` · Figure `figure_7f565db47da99fc1ec26f896` · Article `article_fdc10e3226e65f8bbe1227cf`
Source hash: `b4cc497735fbe69d751afbd80c6811f85563556864f0597071034807ca1ee4cb`

Family: network. Relationship: convergence; shared concepts; divergent outputs. Usage: reference.

**Observed**

- Three differently colored language lanes connect prompt forms to a shared gray concept region and then to corresponding output forms.
- The displayed examples and explanatory wording contain a small opposite-of-large versus opposite-of-small wording inconsistency; this should not be copied into a new diagram.

**Source-context paraphrase**: The article presents an example of shared cross-language processing through interpretable features.

**Purpose**: Make the distinction between language-specific surface form and shared intermediate representation visible.

**Information mapping**: Lane colors identify language; gray nodes identify shared conceptual structure. Convergence and divergence are the important topology, not three isolated models.

**Inferred rationale**: A common middle region makes reuse apparent at a glance. Retaining language lanes on either side helps readers follow which surface forms correspond without making the middle look language-specific.

**Palette**: White explanatory panels with peach framing, restrained language or mechanism colors, tan feature nodes, dark excitation arrows and blue inhibition cues.

**Typography**: Compact sans-serif concept labels with monospaced prompt/output specimens; exact fonts unknown.

**Uncertain reconstruction**: List input forms, shared meaning and output forms separately. Use stable lane colors, neutral shared nodes and labels that agree across all specimens. Verify logical consistency before copying the structure.

**Reusable rules**

- Use convergence/divergence to explain shared processing across surface variants.
- Check internal wording consistency even in an authoritative reference.

**Counterexamples**

- One example does not establish a universal language-independent mechanism.
- A shared node label is not proof of complete equivalence between inputs.

**Cross-domain transfer**

- Domain: Document-format conversion
- Mapping: Several file formats are parsed into a shared semantic model and exported into different presentations.
- Changes: Use actual parsing and output contracts, with failures or lost information labeled when relevant.
- Limits: Do not imply lossless conversion unless the formats and tests support it.

Original note: `runs/curation-04.json#T1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## T2 — An intervention comparison holds the prompt fixed

Source: [Tracing the thoughts of a large language model](https://www.anthropic.com/research/tracing-thoughts-language-model) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2F7032ed7db85b8cd3efe70a89deaf4f15bfe8fc05-1650x900.png&w=3840)

Case `T2` · Figure `figure_cb9a818bef0a2f58c814083f` · Article `article_fdc10e3226e65f8bbe1227cf`
Source hash: `2b92e0ecc0d349eb779c1bf874724da605ff05c472e02a248239098cafdf61e4`

Family: comparison. Relationship: baseline; suppression; injection; observed consequence. Usage: reference.

**Observed**

- Three horizontal rows preserve prompt/output strip positions while changing an intervened concept.
- A magnifier, removal mark and editing mark distinguish baseline inspection, suppression and injection, with explanatory text to the right.

**Source-context paraphrase**: The article describes interventions on internal features and resulting changes in generation.

**Purpose**: Distinguish observation of a feature from changing it and observing an output consequence.

**Information mapping**: Repeated input positions identify a held-constant prompt; the intervention marker identifies what changes; output regions show the corresponding result. The rows are not merely three unrelated generations.

**Inferred rationale**: Matched rows reduce visual confounds and direct attention to the manipulated element. Explicit intervention labels are more informative than color changes alone.

**Palette**: White explanatory panels with peach framing, restrained language or mechanism colors, tan feature nodes, dark excitation arrows and blue inhibition cues.

**Typography**: Compact sans-serif concept labels with monospaced prompt/output specimens; exact fonts unknown.

**Uncertain reconstruction**: Choose neutral original content, specify one manipulated feature or rule and show baseline, removal and replacement in aligned rows. Label actual observations separately from illustrative predictions; do not reproduce the source's poem text.

**Reusable rules**

- Separate inspection from intervention.
- Keep input and output positions stable in a controlled comparison.

**Counterexamples**

- Different prompts would confound the intended intervention comparison.
- A changed output does not justify an unrestricted claim about intent or cognition.

**Cross-domain transfer**

- Domain: Recommendation rules
- Mapping: Show one document recommendation before, after suppressing a criterion and after replacing it.
- Changes: Use an explicit rule engine and actual outputs or clearly hypothetical examples.
- Limits: The example does not establish effects across every document or user.

Original note: `runs/curation-04.json#T2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## T3 — Coarse and precise pathways meet in one result

Source: [Tracing the thoughts of a large language model](https://www.anthropic.com/research/tracing-thoughts-language-model) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Feaabaeb746713f7f82991a0cc6edb091452b2fee-1650x855.png&w=3840)

Case `T3` · Figure `figure_7bcc133d0c9fe3fb8f79607a` · Article `article_fdc10e3226e65f8bbe1227cf`
Source hash: `172aebc1b75dd66c0ee559bab3449908da7a098eaf283f00b400c980a5e768ed`

Family: network. Relationship: complementary pathways; intermediate features; synthesis. Usage: reference.

**Observed**

- An arithmetic input on the left feeds an upper blue pathway and a lower purple pathway before reaching a single result on the right.
- Intermediate feature labels distinguish approximate-range information from final-digit-related information, with shared early gray features.

**Source-context paraphrase**: The article explains an observed addition mechanism through interpretable feature pathways.

**Purpose**: Show how complementary partial computations can contribute to one answer instead of depicting an unexplained monolithic operation.

**Information mapping**: Color distinguishes pathway roles, labels explain intermediate information and the join identifies the common output. The drawing is a simplified mechanism account, not every operation in the model.

**Inferred rationale**: Parallel colored pathways make complementarity easier to inspect than a textual enumeration. Intermediate labels prevent the colors from being merely decorative categories.

**Palette**: White explanatory panels with peach framing, restrained language or mechanism colors, tan feature nodes, dark excitation arrows and blue inhibition cues.

**Typography**: Compact sans-serif concept labels with monospaced prompt/output specimens; exact fonts unknown.

**Uncertain reconstruction**: Define each pathway's information contribution, keep shared early features neutral and label the joining operation. State what has been omitted and avoid inventing edge weights or claiming completeness.

**Reusable rules**

- Explain intermediate representations, not just input and output.
- Use separate pathways only when their informational roles are supported.

**Counterexamples**

- Do not transfer neural-mechanism claims to human reasoning by analogy.
- A simplified network is not a complete computation trace.

**Cross-domain transfer**

- Domain: Data validation
- Mapping: Combine a coarse plausibility filter with an exact checksum-like constraint before accepting a record.
- Changes: Use actual validation rules and explicit rejection behavior.
- Limits: Complementary checks do not necessarily cover every invalid record.

Original note: `runs/curation-04.json#T3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## T4 — Bottom-up causal explanation needs a clear reading direction

Source: [Tracing the thoughts of a large language model](https://www.anthropic.com/research/tracing-thoughts-language-model) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Ffd2e125879ab993949017e03e3465a12fda884bf-1650x857.png&w=3840)

Case `T4` · Figure `figure_fcef0288100c7039c6c9feb0` · Article `article_fdc10e3226e65f8bbe1227cf`
Source hash: `0eb534f32ed425a53042df4a3b153da0c990c3e0048b9e94c4a98e3e7ad0f192`

Family: network. Relationship: two-hop relation; grouped features; upward flow. Usage: reference.

**Observed**

- Prompt tokens occupy the bottom, highlighted place-related tokens connect upward through tan stacked feature groups, and the answer sits at the top.
- Dark maroon arrows connect intermediate geographic concepts inside a white graphic on a peach field.

**Source-context paraphrase**: The article illustrates a multi-hop factual example using feature-level relationships.

**Purpose**: Expose the intermediate relation connecting the question to its answer, rather than suggesting a direct lookup with no internal structure.

**Information mapping**: Upward position expresses the explanatory progression; stacked shapes group features rather than representing multiple agents. Token highlights identify relevant input components.

**Inferred rationale**: A bottom-to-top layout can align with the underlying model explanation, but it needs strong labels because many readers expect left-to-right flow. Grouping reduces complexity at the cost of hiding individual features.

**Palette**: White explanatory panels with peach framing, restrained language or mechanism colors, tan feature nodes, dark excitation arrows and blue inhibition cues.

**Typography**: Compact sans-serif concept labels with monospaced prompt/output specimens; exact fonts unknown.

**Uncertain reconstruction**: State the reading direction, show the input and output anchors and use labeled groups for intermediate relations. Preserve the distinction between a grouped explanatory node and a measured individual mechanism.

**Reusable rules**

- Make unconventional reading direction explicit.
- Label aggregations so stacked shapes are not mistaken for extra actors.

**Counterexamples**

- Do not invent causal weights from arrow thickness.
- A pruned explanatory graph is not a complete model of all alternatives.

**Cross-domain transfer**

- Domain: Product provenance lookup
- Mapping: Connect a product part to its supplier region and then to a relevant handling rule.
- Changes: Use verified relations and label which records support each hop.
- Limits: A relational explanation is not automatically a causal account.

Original note: `runs/curation-04.json#T4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## T5 — Inhibition needs a different edge grammar from excitation

Source: [Tracing the thoughts of a large language model](https://www.anthropic.com/research/tracing-thoughts-language-model) · [Inspected occurrence](https://www.anthropic.com/_next/image?q=75&url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages%2F4zrzovbb%2Fwebsite%2Fbe304d3250c2aab04e19908b3afc9970d1ed7bb0-1650x1004.png&w=3840)

Case `T5` · Figure `figure_038f756d8bcb2b1544c9d232` · Article `article_fdc10e3226e65f8bbe1227cf`
Source hash: `bf8a00a3a0b658c39a48a8cedf4ba995d6329cea49967613def66c23540d58e5`

Family: network. Relationship: matched known/unknown conditions; inhibition; gating. Usage: reference.

**Observed**

- Two aligned graphs compare known and unknown entity conditions, with prompts below and responses above.
- Inactive elements remain in gray ghost positions; dark arrows and blue T-ended links distinguish excitation and inhibition.

**Source-context paraphrase**: The article explains a feature-level example involving known entities, answer production and refusal-related behavior.

**Purpose**: Make gating and suppression legible without reducing every connection to a positive influence arrow.

**Information mapping**: Node positions are held constant between conditions, ghosting marks inactivity and T-ended edges carry a different relation from arrowheads. Double-inhibition logic cannot be read as ordinary forward flow.

**Inferred rationale**: Keeping inactive elements visible preserves a mental map and makes differences local. Distinct endpoint shapes provide semantics that remain interpretable beyond color alone.

**Palette**: White explanatory panels with peach framing, restrained language or mechanism colors, tan feature nodes, dark excitation arrows and blue inhibition cues.

**Typography**: Compact sans-serif concept labels with monospaced prompt/output specimens; exact fonts unknown.

**Uncertain reconstruction**: Define edge types before drawing, give them distinct endpoints and labels, and reuse coordinates across conditions. Keep inactive nodes subdued but legible and include a legend for suppression.

**Reusable rules**

- Use a typed edge grammar for materially different relations.
- Preserve inactive structure when matched comparisons benefit from stable positions.

**Counterexamples**

- Do not turn every inhibitory edge into a generic arrow.
- A selected example does not establish all hallucination or refusal behavior.

**Cross-domain transfer**

- Domain: Rule-based alerting
- Mapping: Compare known and unknown records in an alert engine where a verified condition suppresses an alarm.
- Changes: Use explicit rule semantics, neutral record names and actual activation states.
- Limits: The diagram cannot certify the completeness or safety of the alert policy.

Original note: `runs/curation-04.json#T5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## P1 — Automating the middle preserves research judgment at the ends

Source: [Petri: An open-source auditing tool to accelerate AI safety research](https://alignment.anthropic.com/2025/petri/) · [Inspected occurrence](https://alignment.anthropic.com/2025/petri/figures/petri-alignment-agent-TWITTER-fig-01.jpg)

Case `P1` · Figure `figure_f877389cdd4c4b03883f3238` · Article `article_c6870ed6fa432fb12cdedfa4`
Source hash: `444c455df9ba601c98824cfc2d44a01e660753e97ac29b99e40e55b407d81d9c`

Family: comparison. Relationship: manual versus automated; responsibility preservation. Usage: reference.

**Observed**

- Two tall panels retain hypothesis/scenario regions above and an iteration region below.
- Several blue manual-work stages in the left panel are bracketed into one larger blue Petri region on the right.

**Source-context paraphrase**: The article describes an auditing tool that automates substantial portions of an evaluation workflow.

**Purpose**: Clarify what the tool takes over without implying that choosing a research question or interpreting outcomes disappears.

**Information mapping**: Matched upper and lower regions express retained responsibilities; compression of the middle expresses delegated implementation work. Panel height and collapsed box count do not measure time saved.

**Inferred rationale**: A matched before/after structure isolates the scope of automation more honestly than replacing the entire workflow with one magic box. The retained iteration stage keeps researcher judgment visible.

**Palette**: Pale gray, yellow, green and blue pipeline regions; quantitative figures separately use heatmap, cluster and condition palettes.

**Typography**: Clear phase headings and compact parameter labels; dense statistical figures use many category labels and numeric annotations. Exact fonts are unverified.

**Uncertain reconstruction**: Identify unchanged responsibilities first. Align them in both panels, bracket the actual automated subset and name the automation's input/output contract. Label any measured efficiency separately from geometric compression.

**Reusable rules**

- Show what remains human or externally governed after automation.
- Use matched regions to define the exact scope of a change.

**Counterexamples**

- Collapsed stages do not establish a numerical speedup.
- Do not erase research judgment merely to simplify the automated side.

**Cross-domain transfer**

- Domain: Laboratory instrument workflows
- Mapping: A scientist keeps hypothesis design and interpretation while an instrument automates preparation, execution and aggregation.
- Changes: Name the real automated stages and retain human decisions that the instrument does not make.
- Limits: Automation coverage is not proof of experimental validity or improved accuracy.

Original note: `runs/curation-05.json#P1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## P2 — Maintain case identity through interaction and judgment

Source: [Petri: An open-source auditing tool to accelerate AI safety research](https://alignment.anthropic.com/2025/petri/) · [Inspected occurrence](https://alignment.anthropic.com/2025/petri/figures/petri-alignment-agent-TWITTER-fig-02.jpg)

Case `P2` · Figure `figure_98c20ba39ae555c60324b7df` · Article `article_c6870ed6fa432fb12cdedfa4`
Source hash: `15476f76888cb0853657095ee03fb3a49a0d642ff0bd027685dea10f930d5c8d`

Family: flow. Relationship: scenario seeds; bounded auditing loop; multiple judgments. Usage: reference.

**Observed**

- Seed cards marked A, B and C appear on the left. A central auditing loop connects an auditor, a vertical tool menu and a target.
- Judgment sliders on the right retain case markers, linking scenarios to several assessment dimensions.

**Source-context paraphrase**: Petri uses scenario seeds, an auditing agent, target interactions and subsequent judgments.

**Purpose**: Explain how a seed becomes an interaction and then an assessment while preventing case identities from being lost across stages.

**Information mapping**: A/B/C identifies cases; the tool column is an available action set, not a mandatory sequence. Slider positions are schematic examples rather than experimental measurements.

**Inferred rationale**: Repeated case markers support traceability across the pipeline. A large loop makes interaction central while the separate judge region prevents confusing generated behavior with its assessment.

**Palette**: Pale gray, yellow, green and blue pipeline regions; quantitative figures separately use heatmap, cluster and condition palettes.

**Typography**: Clear phase headings and compact parameter labels; dense statistical figures use many category labels and numeric annotations. Exact fonts are unverified.

**Uncertain reconstruction**: Assign stable case identifiers, enumerate available actions as a menu and label the loop's stopping rule. Repeat identifiers in illustrative outputs and mark those outputs schematic unless backed by data.

**Reusable rules**

- Preserve case identity across transformations.
- Distinguish an action menu from an ordered workflow.

**Counterexamples**

- Do not imply every tool is invoked for every case.
- Do not cite illustrative slider values as benchmark findings.

**Cross-domain transfer**

- Domain: Usability testing
- Mapping: Scenario cards guide a moderator's interaction with a device, after which each case receives several ratings.
- Changes: Use real moderator actions and distinguish observations from judgments.
- Limits: The flow does not establish reliable or valid rating criteria.

Original note: `runs/curation-05.json#P2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## P3 — A triangular matrix removes redundant pairwise information

Source: [Petri: An open-source auditing tool to accelerate AI safety research](https://alignment.anthropic.com/2025/petri/) · [Inspected occurrence](https://alignment.anthropic.com/2025/petri/figures/corellation_matrix.png)

Case `P3` · Figure `figure_48027b55c5de2f3000868d07` · Article `article_c6870ed6fa432fb12cdedfa4`
Source hash: `b1690758c621c7617fbd5df183304c931bf5db71f775df92610613272100de40`

Family: heatmap. Relationship: pairwise agreement; symmetric matrix; restricted scale. Usage: reference.

**Observed**

- Only one triangle of a five-judge matrix is filled; the upper triangle and diagonal are blank rather than populated with duplicate values.
- Red-to-dark-maroon cells contain three-decimal labels and a color bar spanning roughly 0.55 to 0.75. Angled labels identify compared judges.

**Source-context paraphrase**: The article reports pairwise judge agreement using Spearman correlation.

**Purpose**: Let readers inspect agreement among several judges without reading a redundant full square matrix.

**Information mapping**: Each filled cell is a judge pair. Color and text encode correlation; blank symmetric cells are intentionally omitted, not zero. The restricted color range emphasizes variation within the observed interval.

**Inferred rationale**: Triangular layout reduces duplication, while direct labels guard against overreading small color differences. A narrow scale needs explicit labeling because it can visually amplify modest differences.

**Palette**: Pale gray, yellow, green and blue pipeline regions; quantitative figures separately use heatmap, cluster and condition palettes.

**Typography**: Clear phase headings and compact parameter labels; dense statistical figures use many category labels and numeric annotations. Exact fonts are unverified.

**Uncertain reconstruction**: Compute the symmetric agreement matrix from paired observations, retain one triangle, annotate cells and state the statistic and range. Preserve missing-data cells distinctly from omitted symmetric cells.

**Reusable rules**

- Do not encode redundant symmetric values twice when one triangle suffices.
- Label restricted color scales and the exact agreement statistic.

**Counterexamples**

- Agreement between judges is not accuracy against truth.
- Blank diagonal or upper-triangle cells are not zero correlations.

**Cross-domain transfer**

- Domain: Rubric calibration
- Mapping: Compare agreement among assessors applying the same rubric to the same work.
- Changes: Use matched observations and disclose sample size and missing ratings.
- Limits: High agreement can coexist with shared systematic error.

Original note: `runs/curation-05.json#P3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## P4 — Clustering organizes metrics but does not create causal families

Source: [Petri: An open-source auditing tool to accelerate AI safety research](https://alignment.anthropic.com/2025/petri/) · [Inspected occurrence](https://alignment.anthropic.com/2025/petri/figures/dendrogram.png)

Case `P4` · Figure `figure_046b261ba5a7e57a7b729540` · Article `article_c6870ed6fa432fb12cdedfa4`
Source hash: `206b854d65f00344fe91f980d8647ef50a31ec8a3aa98346cfd5a2c2a5dcf5f9`

Family: hierarchy. Relationship: hierarchical clustering; metric similarity; redundancy. Usage: reference.

**Observed**

- A horizontal dendrogram places long metric names on the right and colored branching clusters on the left.
- The horizontal axis is labeled correlation, while the article's explanation discusses converting correlations into distances; the precise convention needs verification before numeric reuse.

**Source-context paraphrase**: The article groups related behavioral metrics using correlation-based clustering.

**Purpose**: Help readers see which metrics may carry overlapping information without inspecting every pairwise comparison separately.

**Information mapping**: Leaves identify metrics; branching expresses a clustering result under chosen similarity and linkage conventions. Cluster colors mark groups, not moral categories or causal mechanisms.

**Inferred rationale**: A tree can compactly expose redundancy among many labels, but it may appear more definitive than the underlying methodological choices warrant. The axis/prose ambiguity should remain a caveat, not be silently resolved by an invented formula.

**Palette**: Pale gray, yellow, green and blue pipeline regions; quantitative figures separately use heatmap, cluster and condition palettes.

**Typography**: Clear phase headings and compact parameter labels; dense statistical figures use many category labels and numeric annotations. Exact fonts are unverified.

**Uncertain reconstruction**: Document the distance transformation, linkage rule and cut criterion before plotting. Keep labels readable and provide the underlying matrix; explain any axis transformation explicitly.

**Reusable rules**

- State clustering conventions, not just the resulting hierarchy.
- Treat grouped metrics as a similarity result rather than causal categories.

**Counterexamples**

- Do not infer a distance formula solely from the axis title.
- A cluster does not prove that all included measures are interchangeable.

**Cross-domain transfer**

- Domain: Questionnaire simplification
- Mapping: Group correlated survey items to inspect potentially redundant questions.
- Changes: Use actual response data and a justified distance/linkage method.
- Limits: Clustering alone cannot validate deleting questions or establish construct validity.

Original note: `runs/curation-05.json#P4`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## P5 — Condition estimates need visible uncertainty and metric definitions

Source: [Petri: An open-source auditing tool to accelerate AI safety research](https://alignment.anthropic.com/2025/petri/) · [Inspected occurrence](https://alignment.anthropic.com/2025/petri/figures/whistleblowing_ablation.jpg)

Case `P5` · Figure `figure_e66dd00f8acc01065717f5d7` · Article `article_c6870ed6fa432fb12cdedfa4`
Source hash: `113153d796bf20b33dd2da0b1562c198d9a3cdadce66aa584fb4f9004c6408c2`

Family: errorbar. Relationship: multi-condition ablation; grouped estimates; uncertainty. Usage: reference.

**Observed**

- Model groups contain several condition-colored lollipop-like estimates with stems, markers and light interval regions/caps.
- The y-axis says mean score, while surrounding explanatory wording discusses a thresholded behavior rate. That metric-label discrepancy requires verification rather than silent reinterpretation.

**Source-context paraphrase**: The article describes a condition ablation and states that uncertainty intervals are shown.

**Purpose**: Compare several variants while allowing readers to see uncertainty rather than treating point ordering as a definitive ranking.

**Information mapping**: Group position identifies model, color identifies condition, marker position identifies the plotted estimate and intervals represent the stated uncertainty. The exact outcome definition must be reconciled with the source before values are reused.

**Inferred rationale**: Intervals are part of the comparison, not optional decoration. Directly recording the metric ambiguity prevents a style study from turning into an unreliable quantitative summary.

**Palette**: Pale gray, yellow, green and blue pipeline regions; quantitative figures separately use heatmap, cluster and condition palettes.

**Typography**: Clear phase headings and compact parameter labels; dense statistical figures use many category labels and numeric annotations. Exact fonts are unverified.

**Uncertain reconstruction**: Obtain the underlying estimates, denominators and interval method; resolve the metric definition; then plot grouped markers with consistent spacing and accessible condition labels. Do not infer significance from point order alone.

**Reusable rules**

- Keep uncertainty visible in multi-condition comparisons.
- Reconcile axis, caption and prose definitions before transcribing numbers.

**Counterexamples**

- Do not call a mean score a rate without verification.
- Small point differences or overlapping intervals are not a complete significance analysis.

**Cross-domain transfer**

- Domain: Training-policy variants
- Mapping: Compare several policy conditions across departments using actual scored outcomes and justified intervals.
- Changes: Define the score or rate precisely and show the appropriate uncertainty calculation.
- Limits: The display cannot correct confounding or inconsistent scoring across departments.

Original note: `runs/curation-05.json#P5`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_image_pixels.

## Q1 — Geometric small multiples connect sparsity to representational packing

Source: [Toy Models of Superposition](https://arxiv.org/pdf/2209.10652) · [Inspected occurrence](https://arxiv.org/pdf/2209.10652#page=2)

Case `Q1` · Figure `figure_90a94588d437982c50f25ba7` · Article `article_d9048ba52b007cf2e825e179`
Source hash: `08fcf99c01fa95a0e9755cdb72775f3b055735a37f0585a9467a457ccb4a11d5`

Family: small_multiples. Relationship: controlled sparsity; representation geometry; interference. Usage: reference.

**Observed**

- Three matched two-dimensional feature diagrams show increasing sparsity, labeled 0, 80 and 90 percent, with an arrow across the top.
- Feature directions progress from fewer separated directions to denser arrangements; a feature-importance legend and interference annotation qualify the geometry.

**Source-context paraphrase**: The paper studies superposition in toy models. A footnote explicitly links reproducible figures to a computational notebook; this supports computational provenance, not a particular font or drawing application.

**Purpose**: Introduce the central capacity/interference tradeoff before detailed model mathematics and experimental grids.

**Information mapping**: Panel identity changes sparsity, vectors represent learned feature directions and their geometry expresses overlap. An origin-coincident feature is not simply missing artwork. Colors identify feature importance rather than panel quality.

**Inferred rationale**: Matched planes permit direct comparison of geometric organization while a small number of features keeps the concept inspectable. The toy-model setting is an essential scope boundary.

**Palette**: Scientific white panels, color-coded feature vectors, red/blue signed matrices and separate sequential feature-related color scales. Palettes have quantitative meanings and must not be treated as interchangeable accents.

**Typography**: Scientific captions, mathematical labels and compact panel annotations. Exact fonts are not established by the screenshot.

**Uncertain reconstruction**: Compute or use verified feature vectors for controlled settings, plot them on common coordinate conventions and preserve importance encoding. Provide an explicit interference explanation and label the toy assumptions; do not hand-place vectors and call them experimental results.

**Reusable rules**

- Use controlled small multiples to explain a geometric mechanism.
- Keep the numerical meaning of position and color tied to the model definition.

**Counterexamples**

- Arbitrary categorical tasks cannot be drawn as mathematically orthogonal vectors without a defined model.
- A toy result is not a complete description of every deployed model.

**Cross-domain transfer**

- Domain: Compressed communication models
- Mapping: Compare verified signal representations under different sparsity assumptions in a limited-dimensional channel.
- Changes: Define the signal model, coordinates and overlap measure, and use its actual vectors.
- Limits: Do not turn a geometric analogy into unsupported claims about people, teams or scheduling.

Original note: `runs/curation-05.json#Q1`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_pdf_screenshot.

## Q2 — Orientation and overcompleteness are different concepts

Source: [Toy Models of Superposition](https://arxiv.org/pdf/2209.10652) · [Inspected occurrence](https://arxiv.org/pdf/2209.10652#page=7)

Case `Q2` · Figure `figure_9b2aaa1b5d834e267b94ffb8` · Article `article_d9048ba52b007cf2e825e179`
Source hash: `cd024ae00b96cace9a8ee47cc0744ff537e39fa8a54d7c2814265753c25e1422`

Family: comparison. Relationship: basis alignment; superposition; interference. Usage: reference.

**Observed**

- Paired vector diagrams contrast directions relative to a privileged basis and a case with more feature directions than dimensions.
- Lower sketches show gray directions, an emphasized active direction and blue projection/interference marks, including an ambiguous combined representation.

**Source-context paraphrase**: The paper distinguishes representational concepts and uses geometric examples to explain superposition and interference.

**Purpose**: Prevent readers from conflating polysemantic orientation with the separate question of packing more features than dimensions.

**Information mapping**: Axes define coordinate conventions, vectors define features and projections describe interference. The paired examples are explanatory schematics, not a prevalence estimate from a production model.

**Inferred rationale**: Separating definitions into matched small diagrams can prevent one vague visual impression from standing for several mathematical claims. Distinct marks for active signal and interference help track the mechanism.

**Palette**: Scientific white panels, color-coded feature vectors, red/blue signed matrices and separate sequential feature-related color scales. Palettes have quantitative meanings and must not be treated as interchangeable accents.

**Typography**: Scientific captions, mathematical labels and compact panel annotations. Exact fonts are not established by the screenshot.

**Uncertain reconstruction**: Write the two definitions first, choose minimal verified vector examples and keep coordinate conventions explicit. Label active signal, represented feature and projection separately. Retain the distinction between schematic explanation and measured learned vectors.

**Reusable rules**

- Give distinct mathematical concepts distinct examples.
- Label the role of a vector or projection, not merely its color.

**Counterexamples**

- A rotated representation alone does not establish overcompleteness.
- Do not treat a schematic projection as an empirical error rate.

**Cross-domain transfer**

- Domain: Signal mixing education
- Mapping: Use a defined two-channel model to contrast basis rotation with multiple signals sharing limited dimensions.
- Changes: Provide the actual linear model and verify projections numerically.
- Limits: Without a mathematical representation, this geometry would be decorative and potentially misleading.

Original note: `runs/curation-05.json#Q2`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_pdf_screenshot.

## Q3 — Aligned quantitative panels expose mechanisms and outcomes together

Source: [Toy Models of Superposition](https://arxiv.org/pdf/2209.10652) · [Inspected occurrence](https://arxiv.org/pdf/2209.10652#page=11)

Case `Q3` · Figure `figure_f855ee9b2c0524f18f1f428b` · Article `article_d9048ba52b007cf2e825e179`
Source hash: `5c5a6e3c90b8597ef5e7426dc18f4e5652fb5146f643ab135f64ba205fb8627c`

Family: small_multiples. Relationship: experimental grid; signed interactions; bias; feature representation. Usage: reference.

**Observed**

- The lower grid repeats matrices, bias strips and feature-length bars across a linear baseline and several sparsity settings.
- Signed red/blue matrix colors differ from the sequential feature-related palette below. Feature order and panel alignment support comparisons across settings.

**Source-context paraphrase**: The paper compares controlled toy-model settings and discusses how sparsity affects learned representations.

**Purpose**: Allow a technical reader to relate several measured aspects of the same setting rather than seeing only a single aggregate outcome.

**Information mapping**: Columns identify settings; vertically aligned views describe different quantities for each setting. Matrix cells, bias strips and bars have different scales and cannot share one casual color interpretation.

**Inferred rationale**: Alignment supports cross-metric reading, but density makes this an expert detail figure. Separate legends prevent signed interactions from being confused with nonnegative feature measures.

**Palette**: Scientific white panels, color-coded feature vectors, red/blue signed matrices and separate sequential feature-related color scales. Palettes have quantitative meanings and must not be treated as interchangeable accents.

**Typography**: Scientific captions, mathematical labels and compact panel annotations. Exact fonts are not established by the screenshot.

**Uncertain reconstruction**: Use one verified experiment table with stable feature order. Generate each quantitative view from its corresponding data, align columns and give each scale its own legend. Supply an overview and accessible numerical appendix rather than shrinking the grid for a general audience.

**Reusable rules**

- Align multiple measurements of the same experimental condition.
- Keep diverging signed scales distinct from sequential nonnegative scales.

**Counterexamples**

- The same color cannot silently mean sign in one panel and quality in another.
- A dense experimental grid is not a suitable editorial cover.

**Cross-domain transfer**

- Domain: Quantitative compression benchmarks
- Mapping: Compare several actual compression settings using interaction matrices, bias diagnostics and per-feature retention measures.
- Changes: Define each measure, reuse feature order and preserve separate scale legends.
- Limits: Do not invent matrix entries or present a new field's data as equivalent to the toy-model experiment.

Original note: `runs/curation-05.json#Q3`. Inspector: ChatGPT multimodal source review; 2026-10-20; official_pdf_screenshot.

Full-PDF version evidence is now retained separately in
[paper-version.json](../examples/full-publication/paper-version.json);
Q1–Q3 inspection records and narrow source hashes remain unchanged.
