# Standing Persona Synopsis

## Summary

MemoryHub compiles a short, user-scoped profile from current memories produced by
conversation dreaming. The profile captures durable information that helps an
agent work with the user—such as preferences, working style, constraints, and
communication needs—without becoming a second transcript or a general-purpose
search result.

The compiled profile is stored as a normal versioned memory node with
`content_type=synopsis`. Its `derived_from` edges identify the source facts used
to build it. A session-start hook loads this profile separately and before its
normal project-memory search.

## Goals and boundaries

- Compile one standing profile per user from their current, active, user-scoped
  `source=dreaming` facts.
- Rebuild the profile after a non-dry-run user-thread dreaming extraction creates
  or updates at least one memory.
- Keep the profile concise (the prompt requests at most 400 tokens and the model
  response is limited to 400 tokens).
- Preserve provenance from the profile to the facts used in that compilation.
- Let the user replace the profile with their own text and preserve that text on
  later compiles.
- Keep synopsis memories out of normal search and list results unless the caller
  explicitly filters for `content_type=synopsis`.

This feature does not inspect every agent chat automatically. It can use only
facts that have already entered MemoryHub through the dreaming pipeline. It does
not replace the conversation thread store, and it does not create memories from
raw thread text on its own.

## Inputs and selection

The compiler queries up to 50 current facts, newest `updated_at` first. A fact
must match all of these conditions:

- same tenant and owner as the requesting user;
- `scope=user` and `source=dreaming`;
- current version, not soft-deleted, and `status=active`.

Project memories, agent-written memories, other users' memories, and old
versions are not inputs. The prompt includes each selected fact's update time
and content. When facts disagree, the newer update wins. If no facts match, the
compiler leaves any existing profile unchanged.

## Model call and output

The compiler uses the same OpenAI-compatible chat-completions endpoint and
configuration as dreaming:

- `MEMORYHUB_CONV_EXTRACTION_MODEL`
- `MEMORYHUB_CONV_EXTRACTION_MODEL_URL`
- `MEMORYHUB_CONV_EXTRACTION_API_KEY`

The URL is the endpoint base; the service appends `/chat/completions`. The
persona-specific system prompt is versioned in `prompts/persona_synopsis.yaml`.
It requests JSON containing a Markdown profile organized into preferences,
working style, values, constraints, communication, and current focus. The
prompt excludes one-off events and diary-style project updates.

The model is not asked to create separate facts. Its only output is the compact
profile text. An empty profile response does not replace an existing profile.

## Stored profile and provenance

The profile is stored as a user-scoped memory with weight `1.0` and
`content_type=synopsis`. A generated profile uses `source=compiler`; a profile
that the user has edited uses `source=user-declared`. Recompiling creates a new
version of the same logical memory, preserving version history.

The profile metadata records the input fact IDs and compile timestamp.
`derived_from` relationships point from the current synopsis version to each
fact used in that compile. Edges to facts no longer selected are invalidated;
edges to facts still selected are retained. When the synopsis is versioned,
normal memory-version logic moves active graph edges to the new version before
the compiler reconciles its provenance set.

## User edits

`memoryhub persona edit "..."` updates the current synopsis, sets
`metadata.user_edited=true`, and marks its source `user-declared`. Future
compiles include that text as a verbatim user-declared section and ensure it
remains in the resulting profile even if the model omits it. The user can run
`memoryhub persona edit` again to replace the pinned text.

The 400-token limit bounds the model-generated response. User-declared text is
preserved verbatim, so a very long manual edit can make the final stored profile
longer than that limit; MemoryHub does not currently measure tokens with a
model-specific tokenizer.

## Invocation and loading

After an extraction run finishes, the dreaming service triggers compilation
only when all of these are true:

1. the run is not a dry run;
2. the thread has `scope=user`;
3. at least one accepted create or update decision produced a memory ID.

Compilation failure is logged and reported separately; it does not undo
successfully extracted memories. Users can also request a rebuild with
`memoryhub persona compile` or MCP action `memory(action="persona_compile")`.
The MCP action is part of the compact profile's `memory` dispatcher; in other
tool profiles, use the CLI command or rely on automatic compilation after
dreaming.

`memoryhub persona show` reads the synopsis through the existing list API.
The Claude hook invokes `persona show --output compact` before its normal search
command. The profile therefore enters the session context as an explicit block
separate from semantically ranked task memories.

Normal search and list calls exclude synopsis nodes by default. A caller can
request them explicitly with `content_type="synopsis"`.

## Schema migration and compatibility

Migration `029_persona_synopsis` expands the `memory_nodes.content_type` check
constraint to allow `synopsis`, and the `source` check to allow `compiler` and
`user-declared`. Its down-revision joins the two current Alembic heads. A
downgrade refuses to proceed while rows use the new values, preventing silent
loss of profile data.

Updating any memory version preserves its existing `content_type` and `source`
unless the caller explicitly supplies replacements. Dreaming explicitly marks
memories it updates as `source=dreaming`, so those facts remain eligible inputs
to future profile compiles.

## Failure behavior and tests

- No eligible facts: return `reason=no_facts`; do not delete or alter an existing
  synopsis.
- Missing model configuration or model request failure: compilation fails. A
  direct compile reports the error; automatic post-dreaming compilation logs it
  without failing the extraction result.
- Empty model profile: return `reason=empty_profile`; leave the existing card
  unchanged.
- User edit: preserve the exact user-declared text through subsequent compiles.
- Tests cover fact filtering, profile fields and provenance edges, pinned edits,
  hidden-by-default retrieval, memory-version metadata preservation, and the
  post-dreaming invocation gate.
