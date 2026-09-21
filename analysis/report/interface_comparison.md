# Interface comparison

Review interface: `analysis/review_app/` (FastAPI plus one static page). Reference: `analysis/server.py` with `analysis/ui/index.html`, run on ten real traces before this was designed. Traces were read live from Langfuse (cached locally); the committed export was not needed as a fallback.

## What reviewing in Langfuse's own view was like

Hard to see at a glance how a conversation flowed. Multi-turn conversations were effectively invisible: each turn is its own trace and nothing ties them together on screen. Everything took a lot of clicking around.

## One design retained from the reference

Margin notes. Select a span of text, type a short note, and the note sits in the right margin beside the highlighted text, inside the context of the conversation. The reference did this well and it is the whole open-coding gesture, so it stayed, along with the plain next / next / next navigation and the clear separation of user, agent and tool blocks.

## One design changed after inspecting the traces

The unit on screen. Cartwheel writes one Langfuse trace per user turn, so in the reference a follow-up turn appeared alone: turn 2 of support-0212 quotes a ticket opened in turn 1, and turn 2 had no tool calls of its own, so it could not be checked. The interface now groups traces by `cartwheel.session_id` (read from the metadata attributes, since Langfuse's session column is empty for these traces) and shows every turn of a conversation top to bottom, each headed by its own trace id so labels still land on the right trace.

A second change came from the same inspection. The reference styled the model's pre-tool sentences ("I'll look up order 3600 to check...") exactly like the final reply, and a first reading mistook them for chatter shown to the customer. Those sentences never reach the user; the server returns one reply per turn. They are now labelled "reasoning · not shown to the user" in lighter italic, and only the reply carries "reply · shown to the user".

## One limitation remaining

A note is anchored by its quoted text, matched to the first occurrence inside its block. If the same phrase appears twice in one block (two identical bullet lines in a reply, say), the highlight lands on the first occurrence, and a note written on the second reads as if it were on the first. The saved record still carries the exact quote and trace id, so nothing is lost, but the on-screen anchor can be off in that case.
