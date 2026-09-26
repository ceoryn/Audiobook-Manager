# Audiobook Manager

Audiobook Manager builds a clean, Audiobookshelf-ready library from an existing
audiobook collection. It discovers books, reconciles duplicate representations,
looks up metadata, joins multipart audio, writes verified M4Bs, and leaves anything
uncertain available for review.

The original library is treated as read-only. New media is written to a separate
output folder, existing output files are never silently overwritten, and questionable
items are quarantined with an explanation instead of guessed.

## Features

- Modern local web dashboard with source and output folder pickers
- Read-only source scanning with cached `ffprobe` results
- Open Library and Audnexus metadata matching with local-metadata fallback
- Multipart MP3/M4A/M4B conversion into a single chaptered M4B
- Verified reuse of clean existing M4Bs
- Parallel scanning and conversion
- Pause, resume, and safe stop controls
- Resumable processing and an SQLite audit trail
- Downloadable diagnostic logs
- Optional Latin-script metadata preference

## Requirements

- Linux
- Python 3.11 or newer
- Node.js 22.13 or newer
- FFmpeg and `ffprobe`

On Arch Linux:

```bash
sudo pacman -S ffmpeg python nodejs npm
```

## Quick start

```bash
git clone https://github.com/ceoryn/Audiobook-Manager.git
cd Audiobook-Manager
python3 -m venv .venv
.venv/bin/pip install -e .
cd web
npm install
cd ..
./audiobook-manager-web
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000), choose the existing source
library and a separate output folder, save the locations, and start processing.
The selected paths are stored only in the ignored local file
`audiobook-manager.json`; no personal library paths are part of the repository.

The output folder may be empty or may already contain earlier verified outputs. It
must not be the source folder, a parent of the source, or a child of the source.

## Command-line processing

The same autonomous workflow is available without the dashboard:

```bash
audiobook-manager process \
  "/path/to/source-library" \
  "/path/to/clean-library" \
  --prefer-latin-metadata
```

Useful options include `--workers`, `--conversion-workers`, `--metadata-threshold`,
and `--database`. Run `audiobook-manager process --help` for details.

## Run the web services separately

Terminal 1:

```bash
audiobook-manager web-api --database library-state.sqlite3
```

Terminal 2:

```bash
cd web
npm run dev -- --host 127.0.0.1 --port 3000
```

The API and dashboard bind to localhost by default. The API can start without a
configuration file; the first folder selection in the UI creates it.

## Optional user services

Example systemd user units are in `systemd/`. A private environment file points them
to the clone, so the repository can live anywhere:

```bash
git clone https://github.com/ceoryn/Audiobook-Manager.git
cd Audiobook-Manager/web
npm install
mkdir -p ~/.config/systemd/user
mkdir -p ~/.config/audiobook-manager
cp ../systemd/audiobook-manager-*.service ~/.config/systemd/user/
cp ../systemd/service.env.example ~/.config/audiobook-manager/service.env
# Edit AUDIOBOOK_MANAGER_PROJECT in service.env to the absolute clone path.
systemctl --user daemon-reload
systemctl --user enable --now audiobook-manager-api.service
systemctl --user enable --now audiobook-manager-dashboard.service
```

## Lower-level inspection commands

Scan a library without processing it:

```bash
audiobook-manager scan "/path/to/library" \
  --database /tmp/audiobook-manager.sqlite3 \
  --report /tmp/library-scan.json
```

Analyze and review a scan:

```bash
audiobook-manager analyze /tmp/library-scan.json --report /tmp/lineage.json
audiobook-manager review /tmp/lineage.json --decision unreviewed
```

Reports and database files are operational data and are intentionally excluded from
Git. Tests use generated temporary fixtures rather than a real audiobook library.

## Safety model

- Source media is never renamed, moved, overwritten, or deleted.
- Source and output folders must be separate and non-nested.
- New files are built in temporary locations and verified before promotion.
- Existing output conflicts are quarantined for review rather than overwritten.
- One corrupt or ambiguous book cannot stop the rest of a run.
- Low-confidence metadata remains reviewable; it is not silently promoted.
- Cleanup and repair utilities default to dry-run plans and reversible quarantine.

The local folder browser is intentionally served only through the localhost API. It
lists directories, not audiobook contents, and configuration changes are disabled
while processing is active.

The API accepts requests from the local dashboard and rejects foreign browser
origins and hostnames. It cannot bind to a public network address. Database and
configuration files must be stored outside the source library.

### Repair and import scripts

Run repository utilities as modules from the clone root, for example
`PYTHONPATH=src python3 -m scripts.plan_remote_to_local_import --help`.
The remote importer accepts nested destination folders on any mounted local
filesystem. Its version 2 plans record the destination device and reserve space;
execution rejects a changed or unavailable device. Older, unversioned import plans
must be regenerated with the current planner and reviewed before execution. Keep
existing provenance ledgers: their format has not changed.

Empty-folder cleanup defaults to a preview:

```bash
PYTHONPATH=src python3 -m scripts.remove_empty_output_directories --audit /path/to/audit.json
# After reviewing the listed folders:
PYTHONPATH=src python3 -m scripts.remove_empty_output_directories --audit /path/to/audit.json --approved
```

Only audited empty folders and parents made empty by their removal are pruned.
Symbolic links, internal folders, and unrelated folders created since the audit
are preserved. Audiobook media is never removed by this utility.

## Development

Run the Python test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Check the web application:

```bash
cd web
npm run lint
npm run build
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/ROADMAP.md](docs/ROADMAP.md) for implementation details.

## License

Audiobook Manager is available under the [MIT License](LICENSE).
