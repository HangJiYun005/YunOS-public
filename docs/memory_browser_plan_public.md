# Memory Browser MVP Public Plan

The memory browser MVP is a local-first interface for exploring imported conversation metadata without exposing real private records in the public repository.

## MVP Goals

- Browse a list of mock conversations.
- Filter by generic tags such as `project`, `study`, `writing`, `tech`, `life`, and `review`.
- Open a conversation detail view backed by mock message snippets.
- Demonstrate search and navigation behavior without using real chat content.

## Data Boundary

The public MVP must use mock data only. It must not include real conversation titles, real message text, real user identity details, raw export files, or generated private databases.

## Suggested Mock Record

```json
{
  "id": "mock-001",
  "title": "Project planning example",
  "updated_at": "2026-01-01T00:00:00Z",
  "message_count": 12,
  "tags": ["project", "review"]
}
```

## First UI Surfaces

- Conversation list with title, update time, count, and tags.
- Search box with local mock filtering.
- Detail panel with placeholder messages.
- Safety banner explaining that real data lives only in ignored local folders.
