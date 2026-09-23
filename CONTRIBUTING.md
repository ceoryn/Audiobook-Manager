# Contributing

Contributions are welcome. Preserve the safety model: source audiobook files are
read-only, new media is built separately and verified, uncertain evidence goes to
review, and destructive operations require an explicit dry-run plan and approval.

Before opening a pull request:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
cd web
npm ci
npm run format -- --check
npm run lint
npm run build
```

Tests must use generated fixtures or temporary directories, never a real audiobook
library. New network providers must be optional, cached, rate-conscious, and tolerant
of failures.
