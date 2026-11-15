# Local Data Policy

AyunOS keeps private working data out of Git by default. Source code, public documentation, and mock demo assets are the intended commit surface.

## Local-Only Directories

These directories are local private storage and are not tracked:

- `data/`
- `imports/`
- `backups/`

They may contain local databases, generated test databases, import packages, intermediate files, and backup copies. These files must remain on the local machine.

## Directory Creation

AyunOS tools create local data directories when they are needed. The repository no longer uses `.gitkeep` files to keep these private directories in Git.

## Public Demo Area

`public_demo/` is the safe public showcase area. It uses mock content only and can be used for website, brand, or product presentation work.

## Database Policy

Default development commands use the sanitized test database. The real local database is never the default development target and must be selected explicitly with a `real` command or an explicit database argument.

This separation keeps development, search, Web UI checks, and mark/tag testing away from private memory data unless the user deliberately opts into real local data access.
