# Jev Browser Chrome — easy Windows setup

This edition lets Jev work in normal tabs that are already open and signed in
inside your personal Chrome profile. It does not launch a separate profile, copy
cookies, or require `chrome://inspect/#remote-debugging`.

## Requirements

- Windows 10 or 11
- Google Chrome
- Python 3.12 or newer
- A TypeSafe Jev API key, or a Vercel AI Gateway key with access to Jev
- An MCP host such as Codex, Grok, Claude Desktop, or another compatible agent

## First-time setup

1. On GitHub, choose **Code → Download ZIP**, then extract the archive to a
   normal folder. You can also clone the repository with Git.
2. Double-click `Install-Windows.cmd`. The installer creates the Python virtual
   environment, installs all packages, generates the extension's local
   connection token, and writes `mcp-config.json`.
3. Open `Settings.cmd`. Choose **Save/change API key** and paste your own key
   into the hidden prompt. The key is never stored as plaintext. Windows DPAPI
   encrypts it for the current Windows account.
4. The installer opens `chrome://extensions`. Enable **Developer mode**, choose
   **Load unpacked**, and select this repository's `extension` folder.
5. Add the server from the generated `mcp-config.json` to your MCP host, then
   restart the host.

If you move the repository later, use `Settings.cmd` to regenerate the MCP
configuration and update the path in your host.

## Usage

Open Chrome and the page you want to use, then ask your agent, for example:

> Use Jev Browser on the open hotel tab. Select the Design category, enable the
> Free cancellation filter, and verify the result.

The host first lists the currently visible tabs, binds the task to the selected
tab and allowed site origins, and starts the Jev loop. Jev chooses each operation
and target. When a field needs free text, the host supplies only the requested
value. At the end, the host independently checks the final page text and
screenshot.

## Updating

Extract the new files over the same folder, run `Install-Windows.cmd` again, and
click **Reload** on the **Jev Browser Bridge** card in `chrome://extensions`.
Reloading is required when the extension version changes.

## Troubleshooting

- **needs_extension:** The extension is disabled, missing, or has not
  reconnected. Enable it and click **Reload** on its extension card.
- **No Jev API key is configured:** Save a key through `Settings.cmd`.
- **The tab is not listed:** Open a normal `http://` or `https://` page. Chrome's
  internal settings pages are intentionally excluded.
- **The MCP tools are missing:** Fully close and reopen your MCP host.
- **Screenshot permission is missing:** Reload the extension after updating it.
  Use extension version 1.0.1 or newer.

## Verified behavior

Live testing on September 20, 2026 covered inventory filtering and sorting, an
editorial form with generated text, and a preferences modal. All three tasks
completed successfully in 3.399–3.904 seconds. The automated suite contains 35
passing tests. These measurements describe the tested machine and pages; they do
not guarantee the same speed or success rate on every website.

This is an independent Windows integration based on
`browser-use/jev-ultrafast`. It is not an unmodified official TypeSafe Windows
product.
