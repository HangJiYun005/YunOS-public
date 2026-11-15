# AyunOS Product Brief

AyunOS is a local-first second brain for turning exported AI conversations and working notes into a searchable personal memory workspace.

## What It Does

- Imports a local ChatGPT export into a local SQLite memory database.
- Preserves searchable conversation metadata and message records on the user's machine.
- Supports search, tags, marks, and a review workflow for deciding what should become long-term knowledge.
- Keeps raw data, generated databases, backups, and private notes outside the public repository.

## Core MVP Workflow

1. Import an export archive locally.
2. Store memory records in SQLite.
3. Search titles and message content.
4. Add tags for lightweight organization.
5. Mark conversations as `project`, `study`, `writing`, `life`, `tech`, `review`, `keep`, or `trash`.
6. Review marked items before turning them into structured notes or project tasks.

## Privacy-First Design

AyunOS is designed around local control. The demo package uses mock data only. Real exports, generated databases, user records, screenshots, and message text are not needed for the public showcase.

## Future Direction

Future versions may add AI-assisted summarization, clustering, topic detection, and richer review flows. Those features should remain opt-in and should preserve the local-first privacy model.

## Demo Boundary

The public showcase site should use only the mock dataset in this package. It should not request, upload, process, or display private user data.
