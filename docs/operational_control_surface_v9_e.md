# YunOS v9-E Operational Control Surface

## Scope

The surface is a private, read-only presentation layer over the v9
Operational State context API. It adds exactly five operational entries:

- Now
- Projects
- Waiting
- Decisions
- Delegations

The existing v8.1 desktop navigation remains available. The operational
surface does not replace the legacy project configuration view.

## Read contract

The browser only issues GET requests to `/api/operational-state/*`. Every
response is delivered with `Cache-Control: no-store`, and the Service Worker
continues to send every `/api/` request directly to the network. Operational
payloads are never added to the v8.2 private conversation cache.

The surface keeps only an in-memory, page-lifetime copy of a successfully read
payload. If a later network request fails, that copy may be shown only with all
of the following visible:

- `Cached`
- `Stale`
- `Read-only`
- the real `cached_at` timestamp

If no page-lifetime copy exists, the surface displays an unavailable state and
does not fabricate a live or cached payload.

## Write boundary

No desktop or mobile operational write control is exposed. Remote isolation
for a browser writer has not been proven, so writes remain restricted to the
controlled `state-admin` path with preview, authority, lease, and commit gates.
Mobile cannot approve, reject, mutate project state, accept an outcome, create
a delegation, clear a blocker, or issue POST, PUT, PATCH, or DELETE requests.

## Presentation contract

The interface follows the existing YunOS design tokens: low-saturation
document layout, generous whitespace, thin separators, cool blue used only for
local state cues, current information first, and technical detail collapsed
under native `details` elements.

The Project resume includes purpose, authoritative baseline, version, phase,
completed/current/next work, decisions, blockers, waiting, delegation,
outcome, state timeline, and source-anchor disclosure. Now remains budgeted and
does not expand full history.

## Accessibility and responsive boundary

- Keyboard navigation and `focus-visible` are retained.
- Every mobile operational button and disclosure target is at least 44 px
  high.
- The mobile panel uses the existing Visual Viewport shell and does not change
  the v8.2 reader or search persistence model.
- Both body and operational content prevent horizontal overflow.
- The global reduced-motion rule remains authoritative.
