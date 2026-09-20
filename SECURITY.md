# Security and privacy

Jev Browser Bridge can read and interact with normal HTTP(S) pages in the Chrome
profile where it is installed. Chrome therefore displays a broad site-access
permission. Install it only from a repository copy you trust and disable it when
you do not want an MCP host to control browser tabs.

The extension connects only to `127.0.0.1` using a randomly generated bearer
token. The token, API keys, run screenshots, and local configuration are ignored
by Git. API keys saved through `Ayarlar.cmd` are encrypted with Windows DPAPI and
can only be decrypted by the Windows account that saved them.

Visible page text is sent to the configured Jev provider for action selection.
Final screenshots and traces stay local. Password, payment, file-upload, hidden,
and disabled fields are not filled by the extension. The MCP server restricts a
run to explicitly supplied origins and rechecks the live page and target before
each mutation.

Do not run two agents against the same tab at the same time. Review consequential
actions such as sending, publishing, purchasing, deleting, or changing account
security before authorizing them.

Please report security issues privately to the repository owner rather than
opening a public issue with secrets or personal screenshots.
