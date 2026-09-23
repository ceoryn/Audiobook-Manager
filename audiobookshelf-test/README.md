# Local Audiobookshelf test

This Compose project runs Audiobookshelf only on the local computer at
<http://127.0.0.1:13378>. The configured audiobook library is mounted read-only.
The `_quarantine` and `_metadata_review` directories are hidden from its scanner.

Configure the host library path and user IDs in the ignored `.env` file, then run:

```sh
docker compose up -d
```

Useful commands:

```sh
docker compose ps
docker compose logs --tail=100 audiobookshelf
docker compose pull
docker compose up -d
docker compose down
```

Audiobookshelf configuration, covers, logs, and its database are stored under the
ignored `runtime/` directory. Removing a container does not remove those files.
