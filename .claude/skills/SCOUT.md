# Scout (source discovery)

> Reference documentation for the Scout agent. Scout is a **runnable Claude
> Code agent** — its live definition is
> [`.claude/agents/Discovery_Agent.md`](../agents/Discovery_Agent.md). This
> file is documentation only, not a runnable agent.

## Purpose

The Scout searches the web and suggests good source pages for a topic you
name, ranked by quality. It exists for the moment when you know a *topic*
("Sponsored Display eligibility") but not *which pages* to feed into the
pipeline.

## When To Use

Refer to this document (or invoke the Scout agent) when the user asks to
*find*, *discover*, or *suggest sources* for a topic. Do **not** use Scout
in the normal pipeline flow: the current production flow starts when the
user provides URLs directly — Scout is available but not automatically
wired into the `fetch <url>` execution path.

## Input

A topic in plain words, e.g. "Amazon Marketing Stream".

## Process

1. Runs a web search for the topic (via the Tavily search tool).
2. Judges each result's quality from its title and snippet — official
   Amazon documentation ranks highest, reputable third-party guides next,
   community sources last, content farms are discarded.
3. Returns a short numbered list of candidate URLs, each tagged
   `[official]` or `[community]`.

## Rules

* Returns ONLY a list of suggested URLs — no files written, no content
  extracted, no knowledge base changes.
* Never fetches full pages; the snippet is enough for its judgment.
* Flags community sources as `[community]` so later stages can treat them
  with appropriate caution.

## Output

A clean list, e.g.
`1. https://advertising.amazon.com/.../amazon-marketing-stream — [official] Stream onboarding guide`.

## Failure Behavior

Empty or poor search results produce a shorter or empty list. Nothing
downstream depends on Scout, so nothing else breaks.

## Example

User: "find sources about Amazon Marketing Stream" → Scout returns 3–6
candidate URLs, which the user can then feed to
`claude -p "fetch <url>, update the bundle"`.

## Implementation

The runnable agent definition is
[`.claude/agents/Discovery_Agent.md`](../agents/Discovery_Agent.md)
(registered under the name `scout`). There is no Python stage for
discovery — the main pipeline begins at Fetch.
