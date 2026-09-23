# Security

Please report security issues privately through GitHub's security-advisory feature
instead of opening a public issue.

Audiobook Manager is designed as a localhost application. Do not expose the Python
API or development dashboard directly to an untrusted network. The folder browser can
enumerate directories available to the operating-system user running the service.

The application does not upload audiobook contents. Optional metadata lookups send
book-identification queries to their configured providers.
