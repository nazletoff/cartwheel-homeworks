# HW1 Part C: one system prompt revision

Video: https://www.loom.com/share/df5abbd8df53418d8ed13bb6203ab40f

## Requirement tested

ESC-1: for a refund above the $100 threshold, the refund tool queues it for human approval and the agent explains that result.

## The failure

The starter prompt's Escalation section gave "a refund above the auto-approval threshold" as its example of when to call `escalate_to_human`. So on order 4455 the agent did both:

1. `issue_refund` correctly queued refund 575 for human approval.
2. The model then also opened ticket 151 for the same refund.

Two human work items for one decision. Record 3 in `hw1-session.jsonl`, prompt version `d108f6949e5e`.

## Why it's a prompt problem

The tools behaved correctly. The spec was clear. The prompt mistranslated it, blurring "queued for human review" (something the refund tool does on its own) with "escalate to a human" (a separate ticket). The model followed the prompt.

## The revision

One sentence replaces the example: an above-threshold refund is not an escalation, `issue_refund` queues it on its own, report that result and do not open a ticket.

## Result

Same request, fresh session, reset database. Record 12, prompt version `8e4ee284767e`: refund queued, no `escalate_to_human` call, and the reply tells the shopper no separate ticket is needed.

## Two notes beyond the edit

**The spec has a gap here.** SPEC.md never says how a queued refund and a support ticket relate, or which queue humans actually work from. That is how the prompt's author blurred them in the first place.

**I also tested RESP-4 with a tool fix, not a prompt fix.** Record 4: merchant 9002 asked for order 4127 (another store's), and the reply confirmed the order exists. I think leaving that discretion to the model is probabilistic, so instead of editing the prompt I changed the `permission_denied` reason string to match the not-found wording. Record 13: the reply no longer confirms the order exists, but still hints it "may belong to another store," inferred from the error code that the spec's TOOL-4 contract requires the tool to return. The durable fix is a lookup scoped to the caller, so out-of-scope and nonexistent orders come back identical. I did not apply it because it changes TOOL-4 and TOOL-8, two course tests, and the `permission_denied` signal that HW2 tracing depends on.
