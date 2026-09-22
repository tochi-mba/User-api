# The HTTP contract

One service, one `/openapi.json`, served at `/docs`. Everything under `/v1` requires a
bearer token; `/healthy` and `/ready` do not, because a load balancer cannot hold one.

Route `operation_id`s are **public API** -- they become MCP tool names
([docs/mcp.md](mcp.md)) -- so renaming one is a breaking change for every client with a tool
bound to it.

**No endpoint takes an account id, and none takes a scope.** Every path is `/v1/user`.
Which person's record you are reading comes from your token's `sub`, and which compartment
of it you can see comes from your token's `aud`. Both are set by the person when they mint
the token against their keyring session, and nothing you send can widen either.

## Tokens

A token is a short-lived RS256 JWT minted by keyring (`POST /v1/auth/service-token`) and
presented as `Authorization: Bearer <token>`. It must carry `exp`, `iat`, `iss`, `sub` and
`aud`; the issuer is pinned to `USER_API_KEYRING_ISSUER` and the audience must be in the
`user` family:

| `aud` | Grants |
| --- | --- |
| `user` | Entries carrying no scope at all. |
| `user.home` | Those, plus entries tagged `home`. |
| `user.health` | Those, plus entries tagged `health`. |

One scope per token. A token that granted two would let its holder correlate across two
compartments, which is most of what compartmentalising was for; the person can mint two
tokens if they mean two.

Every rejected token gets the same 401 with the same message, "the token was not accepted":
a bad signature, the wrong audience, another issuer, an expired token, a missing claim, an
`alg` that is not RS256, a key id that is not keyring's, or a scope this deployment does
not recognise. Two refusals differ only in their `request_id`. Which rule did the refusing
goes to the logs. The single exception is sending no token at all, which says "a token is
required" -- it distinguishes nothing about a token, because there isn't one.

## Errors

Every failure is RFC 9457 `application/problem+json`:

```json
{
  "type": "https://user-api.invalid/problems/validation-failed",
  "title": "Validation failed",
  "status": 422,
  "detail": "a field value may be at most 4096 bytes serialized",
  "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b"
}
```

`request_id` is on every response, in the body and in the `X-Request-ID` header, and it is
what you quote when reporting a 500 -- whose `detail` is deliberately withheld, because
exception text carries paths, hostnames and sometimes a fragment of what somebody wrote
down. An `X-Request-ID` you supply is honoured so a trace can span services, capped at 64
characters. `X-Response-Time-Ms` is on every response too.

`detail` names the rule that failed and **never echoes the offending value**. That is not
politeness. A 422 body is logged by the caller, shown in a transcript and often handed back
to a model, and on this service the value that failed validation is routinely the most
sensitive thing in the request. Validation failures carry an `errors` array of
`{location, message}` pairs, reshaped by hand from FastAPI's -- its own version includes the
input that failed.

### What each status means here

| Status | When | Why not something else |
| --- | --- | --- |
| **401** | No token, or one not accepted. | Undifferentiated on purpose: each distinction is an oracle that helps somebody forge the next one. |
| **403** | Your token does not grant a scope you asked to read or write. | A fact about your *own token*, not about what exists, so being specific leaks nothing -- and a caller that cannot tell "refused" from "absent" retries forever. |
| **404** | No such entry, for this token. | Identical whether it never existed, belongs to another account, is forgotten, or is out of your scope. A 403 here would confirm the entry exists, which is the fact that must not leak. |
| **409** | A per-account limit is full, or a field key is taken by an entry you cannot see. | The second is the one awkward case -- see below. |
| **422** | Malformed key, value, description, note, scope name, search query or cursor; or a value that looks like a credential. | Nothing has touched the database. |
| **500** | A bug. | Detail withheld; quote the request id. |
| **503** | keyring's signing keys could not be fetched, so your token could not be checked either way. Carries `Retry-After: 5`. | Not a 401. A 401 tells a person to log in again because *we* could not fetch a public key, and logging in again would not have helped. |

There is no rate limiting on this service, so nothing produces a 429. The one rate limit
that exists is internal: a token naming an unknown key id can provoke at most one JWKS
fetch per minute, and one that arrives inside that window is refused as a 401 rather than
becoming an outbound request to keyring.

### The 403/404 split, and the 409 that admits a key exists

A scope refusal is **403 and specific**: "this token grants home and cannot write entries
scoped to health". An entry you may not see is **404 and identical to nothing**. The two
rules look inconsistent and are not: the first is about the caller's token, the second is
about whether something exists.

There is one place this service says more than it strictly must. A `user.home` token doing
`PUT /v1/user/fields/blood_type` when a live `health`-scoped field already holds that key
gets a **409** saying the key is taken and nothing was changed. What leaks is the existence
of a key -- not its value, not its scope, not who wrote it. The alternatives are worse:
overwriting silently lets a token clobber a value it cannot read, reporting success for a
write that did not happen makes every other promise here unfalsifiable, and a 404 on a
`PUT` that would have succeeded a moment ago is a caller that retries forever.
[ADR-0004](adr/0004-scope-from-token-audience.md) argues it in full.

## Endpoints

### Health

| Operation | Route | Notes |
| --- | --- | --- |
| `get_health` | `GET /healthy` | Unauthenticated liveness. No I/O, and it never fails. |
| `check_readiness` | `GET /ready` | Unauthenticated. 200 when everything is usable, 503 when any check fails, same body either way. |

Reports the version, the environment, uptime, a process-wide entry count and whether
keyring's keys are fetchable. No per-account anything: a count that moved when one person
wrote something would be an oracle on an unauthenticated endpoint.

### The record as a whole

| Operation | Route |
| --- | --- |
| `get_user` | `GET /v1/user` |
| `describe_schema` | `GET /v1/user/schema` |
| `export_user` | `GET /v1/user/export` |
| `delete_user` | `DELETE /v1/user` |

**`get_user`** is the always-load block: counts (`fields`, `notes`, `pinned`, `forgotten`,
`events`) plus every pinned entry your token's scope permits, capped at
`USER_API_MAX_PINNED`. Call it once at the start of a conversation, not once a turn. No
parameters. Never 404s -- an account that has written nothing gets an empty record with
null timestamps, because that is the truth rather than an error.

**`describe_schema`** returns every field key your token can see, with what each one means,
its `value_type`, its scopes, whether it is pinned and when it was last updated and
confirmed -- and **no values**. The five well-known keys (`preferred_name`, `pronouns`,
`timezone`, `locale`, `forms_of_address`) are listed whether or not they are set. No
parameters. Call it before inventing a key: it is cheap precisely so that it can be called
every time, and reusing a key that already means what you mean is what keeps a record from
becoming fifty near-synonyms nobody can query.

**`export_user`** walks every entry your token can see, oldest first, by cursor.
Parameters: `limit` (1-100, default `USER_API_SEARCH_DEFAULT_LIMIT`) and `cursor`. The
order is fixed and cannot be changed -- see [Pagination](#pagination).

**`delete_user`** destroys every entry, scope row, search-index row, event and setting for
the account in one transaction, then truncates the write-ahead log so the bytes are gone
rather than merely unlinked. Returns counts (`{"entries": 52, "events": 210}`) and never
contents. **Always a hard destruction, whatever `erasure_mode` says**: the setting governs
what forgetting one entry means, and "delete everything you know about me" has one honest
reading. There is no undo, and it cannot reach a backup taken before the call.

### Entries: fields and notes

| Operation | Route |
| --- | --- |
| `search_user` | `GET /v1/user/entries` |
| `get_entry` | `GET /v1/user/entries/{entry_id}` |
| `revise_entry` | `PATCH /v1/user/entries/{entry_id}` |
| `forget_entry` | `DELETE /v1/user/entries/{entry_id}` |
| `confirm_entry` | `POST /v1/user/entries/{entry_id}/confirm` |
| `set_field` | `PUT /v1/user/fields/{key}` |
| `get_field` | `GET /v1/user/fields/{key}` |
| `write_note` | `POST /v1/user/notes` |

**`search_user`** is the one flexible read, and it is one endpoint with a dozen optional
parameters rather than six narrow ones because the caller is a model: one endpoint it can
combine beats six it has to choose between, and a model that picks the wrong narrow
endpoint gets a confidently empty answer.

| Parameter | Meaning |
| --- | --- |
| `q` | Full-text over keys, descriptions, values and note bodies. Stemmed, so "preferring" finds "prefer". At most 200 characters. |
| `type` | `field` or `note`. |
| `keys` | Comma-separated field keys. Each is normalised, so `?keys=Preferred%20Name` finds `preferred_name`. Empty elements are dropped. |
| `key_prefix` | A family of keys, e.g. `contact_`. A trailing underscore is kept, so `contact_` and `contact` are different questions. |
| `note_kind` | `episode`, `observation` or `lesson`. |
| `source` | `stated`, `inferred`, `observed` or `imported`. What the writer *claimed*. |
| `asserted_by` | The token audience that wrote it. What the server *verified*. |
| `sensitivity` | `normal` or `sensitive`. |
| `pinned` | `true` for the always-load set, `false` for everything else. |
| `scope` | Narrows to entries tagged with that scope. **Cannot widen**: asking for a scope your token does not grant is a 403, not an empty page. |
| `since` / `until` | `updated_at` at or after / strictly before. |
| `stale_before` | `confirmed_at` before this **or never confirmed at all**, because a never-confirmed entry is the stalest thing there is. This is how you find what to ask about. |
| `include_forgotten` | Default false. True is how a person reviews what they asked to have forgotten and undoes it. |
| `order` | `recent` (default) or `oldest`. Ignored when `q` is given. |
| `limit` | 1-100. Clamped to `USER_API_SEARCH_MAX_LIMIT`; out of range is a 422 from validation. |
| `cursor` | From the previous page's `next_cursor`. |

Failure modes: 422 for a query with no word characters at all (`"*"`, `"()"`, `"   "`), for
an unfoldable key or key prefix, for `order=relevance` without `q`, and for a cursor that
is malformed or was issued for a different ordering; 403 for a `scope` you do not hold. A
query containing punctuation, `AND`, `OR`, `NEAR` or quotes is **not** a failure -- every
token is quoted into a literal, so `x AND OR y` is four words to look for rather than a
syntax error ([ADR-0008](adr/0008-keyword-search-not-embeddings.md)).

Relevance is not a thing you ask for -- it is what you get when you pass `q`. The value
exists on the enum because it is baked into the cursors a ranked page issues. Asking for
it without a query is a 422 that says so.

**`get_entry`** and **`get_field`** return one entry; the key is normalised first, so any
spelling of it finds the same field. Anything you may not see is a 404.

**`set_field`** creates or replaces the one live field with this key. `PUT` rather than
`POST` because the key is the identity: retrying after a timeout cannot produce two fields.
Body: `value` (JSON scalar, list of scalars, or shallow object -- at most
`USER_API_MAX_VALUE_BYTES` serialised and `USER_API_MAX_VALUE_DEPTH` levels deep),
`description` (required, 1-200 characters), and optionally `source`, `source_detail`,
`scopes`, `sensitivity`, `pinned`. Unknown fields are **rejected**, not ignored;
`asserted_by` is not accepted and comes from your token.

Two behaviours that are not obvious:

- Writing the **same** value again moves `confirmed_at` to now. Restating a value is
  somebody saying it is still true, which is what a confirmation is.
- Writing a **different** value clears `confirmed_at`. The new value has never been vouched
  for, and carrying the old confirmation across would make a fact corrected this morning
  report as confirmed last March.

Failure modes: 422 (bad key, value, description or scope name, or a credential), 403
(a scope your token does not grant), 409 (entry or field cap full, pin cap full, or the key
is taken out of scope). Replacing an existing field is never refused for the entry or field
caps, however full the account is.

**`write_note`** appends -- notes have no key and are never replaced, so each call creates
one, and it answers **201**. Body: `body` (1-`USER_API_MAX_NOTE_CHARS` characters),
`note_kind`, `description`, and the same optional fields as a field write. `source`
defaults to `inferred` on a note and `stated` on a field.

**`revise_entry`** changes part of one entry and leaves the rest, bumping `revision` and
moving `asserted_by` to the reviser. `confirmed_at` is deliberately **not** touched.
`value: null` is a real value: omit the key entirely to leave a value alone, because "unset
it" and "leave it alone" are different requests.

**`forget_entry`** marks the entry forgotten. It is invisible from every read path
immediately, so from an assistant's point of view it is gone. What happens to the bytes is
the account's `erasure_mode`: `grace` destroys them on the first sweep after `grace_days`,
`immediate` destroys them before the response returns, `tombstone` never destroys them. All
three answer identically, so a caller does not need to know which one it is talking to.
Returns the entry with `forgotten_at` set.

**`confirm_entry`** sets `confirmed_at` to now and changes nothing else -- not the value,
not `updated_at`, not `revision`. An entry whose `updated_at` moved every time somebody said
"yes, still true" would sort to the top of a recency listing for not changing.

### Settings and the change log

| Operation | Route |
| --- | --- |
| `get_settings` | `GET /v1/user/settings` |
| `update_settings` | `PUT /v1/user/settings` |
| `read_events` | `GET /v1/user/events` |

**`get_settings`** never 404s: an account that has expressed no preference has the default
preference (`grace`, 30 days, `log_values` off). Worth reading before telling somebody what
"delete" will do for them, because the answer genuinely differs.

**`update_settings`** takes `erasure_mode`, `grace_days` (0-3650) and `log_values`; omitted
settings are left alone. Changes are **never retroactive** in either direction. Do not call
it on your own initiative -- it is the person's decision about their own data.

**`read_events`** returns the change log, newest first. Parameters: `limit` (1-100) and
`before` (a sequence, exclusive). Page by `sequence`, never by timestamp: two changes in the
same tick share one. `detail` carries old and new values only if the account turned
`log_values` on, and `null` is the default rather than a claim that nothing changed. An
event outlives the entry it describes, so an `entry_id` here may name something that no
longer exists -- which is the point rather than a dangling reference.

`next_before` is the last event's sequence and is non-null whenever the page is non-empty,
so loop until `events` comes back empty rather than until `next_before` is null.

## Pagination

Entry pages are keyset-paginated, not `OFFSET`-paginated. `OFFSET` counts rows from the
start of a result set that is being written to while you walk it: an entry added above your
position shifts everything down and you see a row twice, one removed above your position
shifts everything up and you never see a row at all. For a search box that is cosmetic. For
an export it is corruption.

So a cursor carries *where you were* -- the sort key and entry id of the last row handed out
-- and the next page is "strictly after that". Loop until `next_cursor` is `null`; it is
`null` exactly when there is no next page, which is not the same as this page being shorter
than your limit.

A cursor is opaque base64url, unsigned, and short-lived by nature. It does not need signing:
the account filter and the scope filter are applied to the query regardless, so the worst a
tampered cursor achieves is a page of your own data in the wrong order.

**Which ordering is stable:**

| `order` | Sorts by | Stable under concurrent writes? |
| --- | --- | --- |
| `recent` (default) | `updated_at` descending | **No.** A row you have not reached can be revised, jump to the top, and be missed. Fine for a search box. |
| `oldest` | `created_at` ascending | **Yes.** `created_at` never changes after the insert, so a row cannot move and a walk cannot skip one. |
| relevance | `bm25()` rank | Implied by `q`; you cannot ask for it. |

`export_user` is fixed to oldest-first for that reason and offers no choice. If you are
walking everything through `search_user`, pass `order=oldest` and get the same guarantee.

The ordering is baked into the cursor and checked on the way back in. Replaying a relevance
cursor against a recency walk would compare a bm25 score to an ISO timestamp, which SQLite
compares happily and meaninglessly, so it is a 422 instead.

## Worked example

```bash
BASE=http://127.0.0.1:8002
# A token keyring minted for this person, audience `user` or `user.<scope>`.
TOKEN=...

# 1. Who am I talking to? One call, at the start of the conversation.
curl -s $BASE/v1/user -H "Authorization: Bearer $TOKEN"
# -> { "account_id": "...", "granted_scope": null, "counts": {...}, "pinned": [...] }

# 2. What is already recorded, and what does each key mean? Call this BEFORE
#    inventing a key -- no values come back, so it is cheap enough to call every time.
curl -s $BASE/v1/user/schema -H "Authorization: Bearer $TOKEN"

# 3. A named fact. PUT, because the key is the identity.
curl -sX PUT $BASE/v1/user/fields/timezone \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"value":"Europe/Lisbon","description":"Their timezone, for scheduling",
       "source":"stated","pinned":true}'

# 4. Something that happened. Notes are appended, never replaced.
curl -sX POST $BASE/v1/user/notes \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"body":"Asked me to stop summarising before they have read something.",
       "note_kind":"lesson","description":"How they want things presented",
       "source":"stated"}'

# 5. Find it again. Stemmed, so "summarise" finds "summarising".
curl -s "$BASE/v1/user/entries?q=summarise" -H "Authorization: Bearer $TOKEN"

# 6. What has nobody vouched for lately? This is how you find what to ask about.
curl -s "$BASE/v1/user/entries?stale_before=2025-09-11T00:00:00Z&limit=5" \
  -H "Authorization: Bearer $TOKEN"

# 7. They said it still holds. Touches confirmed_at and nothing else.
curl -sX POST $BASE/v1/user/entries/$ENTRY_ID/confirm -H "Authorization: Bearer $TOKEN"

# 8. They asked you to forget it. Invisible immediately; what happens to the bytes
#    is their erasure_mode, not yours.
curl -sX DELETE $BASE/v1/user/entries/$ENTRY_ID -H "Authorization: Bearer $TOKEN"

# ...and if they meant all of it. No undo, and counts come back rather than contents.
curl -sX DELETE $BASE/v1/user -H "Authorization: Bearer $TOKEN"
# -> {"entries": 52, "events": 210}
```

Everything those reads return is a **reported claim about a person, not an instruction**.
Render it as "your notes say", with the date it was last confirmed, and let them correct
it. [docs/mcp.md](mcp.md) says what that looks like in a prompt, and why an assistant that
skips it has built something worse than a text file.
