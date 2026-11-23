# Changelog

## Unreleased

- Add a safe official DSH updater using the signed stable feed, SHA-512 validation, an atomic program-directory swap, and a rollback backup. DSH homes, sessions, plugins, accounts, environments, and browser data stay outside the replaceable runtime.
- Add a right-side conversation dock for “我的环境” with floating and fixed-insert modes.
- Add native single- and multi-select support to Agent browser tasks, with option metadata in observe results and a controlled-form fallback when a platform popup keeps keyboard focus.
- Add date/time input handling for native calendar controls and a re-locating menu_select action for cascading menus with multiple levels.
- Add replaceable text input, clear and keyboard actions, desired checkbox/radio state, range controls, scroll-into-view, file upload, and state-aware action summaries.
- Add Agent tab lifecycle controls for opening, switching, and closing tabs inside the selected environment.
- Capture JavaScript alert, confirm, and prompt events and expose an explicit dialog action so blocked pages do not receive accidental follow-up input.
- Observe same-origin iframe and open Shadow DOM controls with context paths, and map nested targets back to top-level trusted click coordinates.
- Add trusted drag paths for sortable controls, drop zones, and pointer-driven sliders.
- Add per-environment download waiting with a private download directory and completed-file verification.
- Add explicit clipboard read/write and accessibility-tree inspection capabilities for tasks that need semantic page context.
- Extend wait_for with value, checked, selected, expanded, iframe, and Shadow DOM conditions for post-action verification.
- Add an infinite-canvas action protocol for node movement, connections, disconnections, editing, panning, zooming, selection, and Escape cancellation.
- Recognize visible div-based dropdown triggers such as `wj-select` and `select-content-wrap`, deduplicate nested visual wrappers, and move the pointer over custom menu items before clicking them.
- Keep the custom-menu hover settle window at 40 ms after measurement, reducing each cascading level without sacrificing the real Wenjuan dropdown selection.
- Rebind the native DSH overlay to the new authenticated Web URL after an idle DSH Web restart, preventing the assistant panel from disappearing after a service reload.
- Make cascading menu selection tolerate an already-open popup and labels with visual prefixes such as `A.` or `2.`, avoiding the trigger toggle and false `MENU_ITEM_NOT_FOUND` retries.
- Observe custom scale, star, NPS, radio, and geolocation controls as actionable choices, including the location permission handoff.
- Keep rendered controls outside the current viewport in observe results and automatically scroll them into a stable visible position before Agent actions.
- Refresh Agent observations after scrolling so virtualized lists and lazy-loaded form controls can be located and filled without stale element ids.
- Raise the default Agent observation window to 160 controls, so late questionnaire fields remain discoverable after long runs of earlier inputs.
- Make infinite-canvas actions validate finite coordinates and the selected board, focus the board before keyboard follow-up, use complete double-click cycles for editing, and preserve the zoom anchor while panning and zooming.
- Add a ten-round graph-editor regression that verifies node selection, connections, disconnections, editing, cancellation, layout-shifted coordinates, panning, anchored zooming, and invalid-input rejection.
- Keep fixed-insert conversation docks persistent when the user clicks the page or other browser content. Page input continues normally without folding the dock.
- Keep the DSH sidebar and conversation interactive while the middle browser page remains visible and clickable.
- Keep DSH full-permission confirmation dialogs fully interactive, including releases that render the checkbox panel without dialog semantics.
- Remove stale loopback DSH authentication cookies during browser startup and refresh the current workbench URL, preventing accumulated `dsh-auth-*` cookies from causing HTTP 431 or an authentication-required screen.
- Add a native compositor-side Agent pointer for move, click, focus, typing and scrolling. It is isolated per environment and does not modify page content or intercept page input.
- Show the native pointer for direct Agent navigation as well, before dispatching the real page navigation.
- Refine the native pointer appearance with a light motion trail and click-only ripple, removing the persistent outer ring during ordinary movement.
- Add a low-latency scroll action to the browser control bridge with the same native pointer feedback.
- Keep custom development profiles aligned across the browser, bridge, and environment services.

## 1.0.6 - 2026-11-12

- 网络代理使用 IP、端口、用户、密码四字段，支持粘贴自动拆分；凭据通过 Windows DPAPI 保存在本机。

- Give the browser and DSH services a persistent product-private temporary directory instead of inheriting disposable installer paths.
- Isolate embedded DSH Web, SDK and plugin initialization from another Desktop's Electron/Node carrier variables and bridge credentials.
- Bind the embedded DSH Web service explicitly to loopback with an OS-assigned port.
- Check the authenticated Agent preset API as well as the HTML shell before reporting DSH healthy.
- Add disposable two-backend regressions for simultaneous API requests, prompt admission, WebSocket connectivity, authentication isolation and independent shutdown.
- These local checks do not establish that every standalone DSH Desktop coexistence issue or online model workflow has been resolved.

## 1.0.5 - 2026-11-11

- Improve Windows browser startup compatibility without disabling the renderer sandbox, and report early startup failures accurately.
- Add explicit page readiness waits and actionable element checks without requiring every background resource to finish loading.
- Reject stale observed element IDs before sending input and avoid replaying actions after uncertain outcomes.
- Combine observation and baseline summaries in one renderer traversal and bypass system proxies for loopback control traffic.
- Detect outdated bridge processes, replace confirmed idle owned instances, and preserve busy tasks and other installations.
- Distinguish an unavailable browser control connection from a stopped environment and stop unnecessary reopen/shell fallback loops.
- Bundle the product README next to its license notice and update Windows installer, launcher and assistant menu versions to 1.0.5.

## 1.0.4 - 2026-11-02

- Include H.264/AAC playback support, with MP4, MSE/DASH and VP9/Opus playback regression checks.
- Stabilize authenticated proxy negotiation with fragmented TCP replies; preserve reverse responses after a TCP half-close and clean up connections on shutdown.
- Reduce full environment catalog polling, pause it in hidden documents, and keep immediate updates after local edits.
- Preserve clean first-run data, show setup guidance only on first use, and improve assistant/onboarding and environment panel behavior.
- Include the environment check page, saved account details and initial connectivity fixes in the published application sources.
- Show the BlueBean product version and contact email in the lower-left DSH menu and General settings.
- Add a shared product version and Windows executable version metadata.

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
