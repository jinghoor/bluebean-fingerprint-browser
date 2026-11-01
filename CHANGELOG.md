# Changelog

## 1.0.1 - 2026-11-02

- Correct final local installer integrity labels inherited from restricted build folders; verify labels using Windows SIDs after the last delivery copy.
- Detect a Low-integrity installer before extraction and report an actionable error (silent exit 13).
- Configure sandbox file access with a native GUI helper instead of a hidden console command; retain existing ACLs and apply inherited read/execute entries only to the installation tree.
- Return failure code 20 if sandbox permission configuration fails.
- Keep the raw Inno payload separate from the final executable during rebuilds.
- Add normal-install checks for uninstall registration, real shortcut targets/icons, installed integrity and sandbox file access.

## 1.0.0 - 2026-11-01

- First packaged Windows x64 installer release.
- Bundled Chromium runtime, DSH, Python, Node.js and application dependencies.
- Added isolated browser environments, local account and proxy management, DSH agent APIs, and native environment sidebar.
- Installer verified on the build machine with sandbox rendering, DSH first-run plugin setup, private runtime checks and data-preserving uninstall.
