# Monash Moodle Downloader

A Python command-line tool for reading and synchronising course resources that the user is
authorised to access on Monash Moodle.

The project is being built around a browser-authenticated Monash session and Moodle AJAX web
services. Course downloads, browser profiles, cookies, local databases, and temporary files are
kept outside Git.

> Status: browser login, session checks, and Moodle AJAX course discovery are implemented.
> Course-page parsing and downloads are added in later stages.

## Requirements

- macOS
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Google Chrome (required once browser login is implemented)

## Set up the development environment

```console
uv sync --all-groups
uv run mmd --help
```

## Commands

```console
mmd login
mmd doctor
mmd courses
mmd scan --course FIT2014
mmd sync --course FIT2014
mmd sync --course FIT2102 --week 3
mmd sync --all
mmd sync --course FIT2014 --refresh
```

`login`, `doctor`, and `courses` are available now. The `scan` and `sync` interfaces are present
but remain disconnected until the parsing and download stages. The normal `sync` command requires
either one course or the explicit `--all` option. The default material directory is
`~/Desktop/Monash Moodle Downloads`.

The login command opens a dedicated Chrome profile. Complete Monash SSO and MFA in that browser
window; the CLI never asks for credentials. Later non-interactive commands reuse the saved profile.

## Development checks

```console
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Privacy and safety

- Complete Monash SSO and MFA only in the visible browser window.
- Never enter a Monash password or verification code into this command-line tool.
- Do not commit course materials, browser profiles, cookies, session keys, or local databases.
- Only access and download course material that your own Monash account is authorised to use.
