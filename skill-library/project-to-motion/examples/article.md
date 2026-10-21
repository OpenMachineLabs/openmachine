# One request, one useful path

A support router receives one customer request and classifies its intent. A billing request goes to the billing specialist. A technical request goes to the technical specialist. This example chooses exactly one specialist; it does not send the same request to both.

For the billing example, the specialist first checks the account, then identifies the relevant invoice, then returns an explanation to the customer. The explanation depends on those earlier steps.

Routing and parallel work solve different problems. Routing selects a suitable path for one input. Parallel work sends independent tasks to multiple workers and combines the required results. Neither pattern establishes a speed improvement without measurement.

The useful question is not how many boxes a system contains. It is why this request takes this path, and what result that path makes possible.
