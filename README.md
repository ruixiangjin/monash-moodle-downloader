# Monash Moodle Downloader

A Python command-line tool that saves authorised Monash Moodle course text and non-media files for
offline study. It combines Moodle AJAX course data with browser-readable pages, so it does not
depend on a Moodle REST token or one fixed course layout.

## What it saves

- Week headings and useful course, Page, Text and media, and Assignment text as Markdown.
- File, Folder, and Assignment attachments such as PDF, Office, CSV, text, code, JAR, and ZIP.
- A versioned `manifest.json` containing the detected structure and sync result.
- External links as references; a confirmed direct non-media file may be downloaded.

Images, video, audio, fonts, Canva, Panopto, YouTube, H5P, and ordinary external webpages are not
downloaded. ZIP files are saved without extraction, and downloaded documents are not converted.

## Requirements and installation

- macOS with Google Chrome
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)

```console
git clone https://github.com/ruixiangjin/monash-moodle-downloader.git
cd monash-moodle-downloader
uv sync --all-groups
uv run mmd --help
```

Use `uv run mmd ...` from the repository, or run `uv tool install .` once if you want the shorter
`mmd ...` command everywhere.

## First login

```console
uv run mmd login
uv run mmd doctor
uv run mmd courses
```

`login` opens a dedicated Chrome window. Complete Monash SSO and MFA only in that window. The tool
never accepts your password or verification code. If a later command reports that the session has
expired, run `mmd login` again.

## Scan and synchronise

```console
# Read structure and text without downloading attachment bodies
uv run mmd scan --course FIT2014

# Incrementally synchronise one course or one teaching week
uv run mmd sync --course FIT2014
uv run mmd sync --course FIT2102 --week 3

# Synchronise every course currently returned by Moodle
uv run mmd sync --all

# Ignore remote metadata and fetch file bodies again
uv run mmd sync --course FIT2014 --refresh
```

A normal sync must specify exactly one `--course`; all courses are only selected by the explicit
`--all` option. Course codes and Moodle numeric IDs are both accepted.

The default output is `~/Desktop/Monash Moodle Downloads` and has this shape:

```text
Course name/
├── README.md
├── manifest.json
├── Week 01 - Title/
│   ├── README.md
│   ├── Files/
│   └── Assignments/
└── General/
```

Use `--output /another/folder` on `scan` or `sync` to choose a different material directory.

## Incremental behaviour

The private SQLite state records ETag, Last-Modified, size, SHA-256, and local path. A later sync
uses remote metadata before requesting the file body:

- unchanged local files are not downloaded again;
- changed files and manually deleted local files are downloaded again;
- interrupted transfers use temporary `.part` files, which are removed instead of being presented
  as complete;
- files removed from Moodle are marked `missing_remote` without deleting the local copy;
- `--refresh` deliberately bypasses the unchanged check.

Each sync prints a summary for downloaded, unchanged, skipped-media, unsupported, missing, and
failed resources. The generated Markdown links point to downloaded files using relative paths.

## Privacy and repository safety

Browser state, cookies, and SQLite data live under macOS Application Support. Downloaded materials
live outside the repository, and `.gitignore` excludes common credentials, databases, partial
files, and output directories. Exported manifests remove common token and temporary-signature query
parameters.

Before publishing changes, still review `git status` and do not commit course materials or login
data. Only access material that your own Monash account is authorised to use.

## Troubleshooting

- **Login required:** run `uv run mmd login`, finish SSO/MFA, then retry.
- **Course not found:** run `uv run mmd courses` and use the displayed code or numeric ID.
- **A link is recorded but not downloaded:** it is likely HTML, media, Canva, Panopto, H5P, or an
  external page requiring another login. This is intentional in the first version.
- **A file failed:** retry the sync. Completed files remain intact and incomplete `.part` files are
  removed.
- **Need a clean re-fetch:** add `--refresh`; this consumes more bandwidth.

## Development checks

GitHub Actions runs only anonymous, offline fixtures and simulated HTTP responses. It never signs in
to Monash or downloads real course material.

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```
