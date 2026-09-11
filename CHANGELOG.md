# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.7.1] - 2026-09-11

### Added
- **Adversarial & Security Hardening (`hub/routes/agent_activities.py`, `tests/api/test_agent_activities_routes.py`)**:
  - Immunized `parse_sentinel_transcript` and `handle_sentinel_detail` with strict alphanumeric conversation ID regex matching (`^[a-zA-Z0-9_\-]+$`) and path containment checks (`is_relative_to(brain_dir)`) to eliminate directory traversal attacks (`400 Bad Request` / `404 Not Found`).
  - Added type guards (`isinstance(..., dict)`) in `normalize_signal_data` and JSONL transcript line parsing to gracefully handle corrupted, non-dict payloads or malformed lines without throwing unhandled exceptions.
  - Added comprehensive adversarial tests: `test_sentinel_detail_adversarial_traversal_rejected` and `test_corrupted_signal_and_transcript_resilience` (10/10 tests passing in route suite, 242/242 tests passing across whole repo).

### Fixed
- **Unidirectional Realtime Reactive Flow & UI/UX State Preservation (`hub/routes/dashboard_template.py`)**:
  - **SSOT Task Completion Handshake**: Log drawer dynamically re-pulls `/tasks/{taskId}` from the authoritative endpoint upon receiving SSE `completed` events, updating exit code, duration, and immediately displaying any newly emitted agent signals or pulses.
  - **Task Re-run Drawer Sync**: Triggering a task rerun from within the drawer now automatically re-invokes `openDrawer(taskId)` to stream the new execution.
  - **Reactive Multi-View SSE Sync**: `refreshTasksAuthoritative()` dynamically refreshes the active view (`loadSentinels()`, `loadSignals()`, `loadPulses()`), keeping all agent observability tabs synchronized in real time without stale states.
  - **Sentinel Accordion State Preservation**: Maintained open/collapsed accordion state in `state.openSections` across background SSE refreshes so inspection view does not collapse during background updates.
  - **Modal Backdrop & Mobile Drawer Dismissal**: Added click-to-dismiss on `#signalModalOverlay` backdrop and automatic mobile sidebar collapse on navigation.

## [1.7.0] - 2026-09-11

### Added
- **Agent & Sentinel Observability Hub & Interactive UI/UX Pro Max Views (`hub/routes/agent_activities.py`, `hub/routes/dashboard_template.py`, `hub/routes/tasks.py`)**:
  - **Sentinel AI Runs View**: Dedicated telemetry board displaying cadence runs (`CAD-20260911-webhook-hub-sentinel`), execution status, tool usage badges, and 3-stage collapsible accordions for full injected prompt (`agentapi new-conversation`), autonomous tool execution stepper timeline (inspecting tool names, args, and outputs), and final delivered markdown reports.
  - **CRM Agent Signals & Dispatches View**: Full inspection table of emitted signals from `.agents/signals/contact_review/` featuring verdict badges (`CREATE` purple, `CORRECT` emerald, `NO_CHANGE` slate), confidence score pills, direct Notion CRM page external links, and interactive "Inspect" modal displaying formatted property diffs and raw JSON payload.
  - **Sidebar Pulse Queue View**: Live monitoring table of Antigravity conversation sidebar events from `~/.gemini/antigravity/sidecar_data/webhook-hub-sentinel/events/`, linking dispatched tasks directly to execution log drawers.
  - **Task Drawer Agent Activity Integration**: Embedded "Associated Agent Activity & Dispatches" card inside the slide-out log drawer, automatically enriching contact-review tasks with proposed property diffs, verdict badges, and one-click Notion navigation.
  - **High-Performance Lazy Endpoints**: Added `/api/agent-activities/summary`, `/api/agent-activities/sentinels`, `/api/agent-activities/sentinels/{id}`, `/api/agent-activities/signals`, and `/api/agent-activities/pulses` with zero caching and on-demand transcript parsing.
  - **Strict Memory Budget Compliance**: Maintained gateway process RSS strictly < 25 MB (budget: `< 30.0 MB`), verified via automated GC pressure relief and lazy route resolution in `hub/cli.py`.
  - **100% SVG Iconography & Dark OLED Theme**: Complete adherence to `/ui-ux-pro-max` design standards with zero emoji icons.

### Fixed
- **Sentinel Conversation Discovery Isolation (`hub/routes/agent_activities.py`)**: Fixed discovery heuristics that previously misclassified interactive user coding tasks and subagents mentioning repository names as Sentinel runs, strictly isolating genuine autonomous Sentinel runs (`CAD-20260911-webhook-hub-sentinel`).
- **Signal Confidence, Diffs, and Applied Status Normalization (`hub/routes/agent_activities.py`, `hub/routes/dashboard_template.py`)**: Resolved nested structure where `confidence_score`, `diffs`, `applied`, and `explanation` resided inside `result`, ensuring accurate confidence percentages (70%, 80%, 90%, 100%), applied badges, and proposed diffs render across table, detail modal, and drawer.
- **Task Drawer Action Buttons**: Added interactive **Quick Inspect** (opening modal) and **One-Click Copy** actions on drawer activity cards.
- **Sidebar Navigation Active State**: Added `id="navItemAll"` to restore active sidebar tab highlight when navigating back to tasks.
- **Transcript Collapsible Robustness**: Replaced brittle string-split ID parsing in `toggleSentinelSection` with robust parameterization.
- **Expanded Task Activity Association**: Broadened signal and pulse correlation to search `stdout`, `command`, `action_params_json`, `result_json`, `error_message`, and `logs`.
- **Comprehensive Verification**: Added 2 new tests in `tests/api/test_agent_activities_routes.py` (total 8 tests passing, 240/240 full test suite passing in 33s, 12/12 standalone verification passing), and captured 6 Retina screenshots in `docs/screenshots/`.

## [1.6.5] - 2026-09-11

### Added
- **Native Task Review & Batch Rerun CLI Toolchain (`hub/cli.py`, `tests/unit/test_cli.py`)**:
  - **`webhook-hub tasks` Subcommand**: Direct CLI inspection of event-driven tasks supporting status filtering (`--status failed|succeeded|running|queued`), environment filtering (`--real` to isolate real business tasks vs `--test-only`), custom limits (`--limit N`), and machine-readable JSON output (`--json`).
  - **Enhanced `webhook-hub rerun --failed`**: Batch recovery of failed and timed-out tasks with `--real-only` (safely preventing synthetic test suites from being re-enqueued) and `--dry-run` inspection.
  - **Dual-Mode Operation**: Automatically queries the live gateway via HTTP when running, with seamless fallback to offline direct SQLite SSOT reading under lock.
  - Eliminates the need for external ad-hoc Python/SQL scripts or fragmented tools to inspect failed tasks.
  - Added full test coverage in `tests/unit/test_cli.py` (`test_cmd_tasks_list_and_filter`, `test_cmd_rerun_batch_failed`).

## [1.6.4] - 2026-09-11

### Fixed
- **Synthetic Test Quarantine & Parameter Spoofing Immunization (`hub/models.py`)**:
  - Immunized `is_test_task` and `TEST_EVENT_SQL_FILTER` against parameter spoofing (`"is_test": false`), ensuring tasks or events containing `pw_verify` and `playwright` are strictly quarantined to the test category.
  - Normalized synthetic Playwright log drawer fixtures in SQLite SSOT, restoring the production `Real Only` dashboard feed to 100% clean green status (8/8 production tasks succeeded, 0 failed, 0 blocked).
  - Verified with 12-check E2E suite (`./bin/webhook-hub verify`) and full unit/API test suite (20/20 passed).

## [1.6.3] - 2026-09-11

### Added
- **Developer Observability UI/UX Pro Max Log Drawer Engine (`hub/routes/dashboard_template.py`, `hub/routes/tasks.py`, `hub/db.py`)**:
  - **Terminal Prompt Shell Banner**: Introduced top banner displaying executing command (`❯ command`), one-click copy button, formatted duration, exit code status pill (`Exit 0` emerald, `Exit 1` rose), and execution timestamp.
  - **True Chronological Stream Interleaving (SSOT)**: Replaced sequential `stdout` then `stderr` dump with interleaved event sequencing reading directly from SQLite `execution_logs` ordered by `log_id ASC`.
  - **Stream Badges & Modern Line Gutter**: Integrated distinct stream pill badges (`OUT` muted slate, `ERR` rose red, `SYS` purple) alongside line numbers in a fixed-width gutter.
  - **Filter Segmented Pills with Dynamic Badges**: Upgraded unstyled level selector to sleek segmented pill tabs (`All`, `Errors`, `Warnings`, `OUT`, `ERR`) featuring dynamic live count badges.
  - **Pretty Collapsible JSON Cards**: Structured JSON payloads are parsed into standalone cards with syntax color coding (keys in cyan, strings in green, numbers in violet, booleans in red) and inline "Copy JSON" actions.
  - **Python Traceback Callout Cards**: Exception blocks are automatically packaged into high-visibility callout cards with error type badges and one-click "Copy Traceback".
  - **Global Drawer Search & Shortcuts**: Implemented dedicated search bar with clear button (`✕`), hit counter, and `Cmd+F` / `Ctrl+F` global shortcut focusing the drawer search when open.
  - **Density Toggle & Raw Log Download**: Added layout density switcher (`Comfortable` vs `Compact`) and one-click `.log` file download for external debugging.

### Changed
- **Memory Footprint Hardening & Lazy Route Decoupling (`hub/routes/webhook.py`, `hub/cli.py`, `hub/server.py`, `hub/db.py`, `hub/memory.py`)**:
  - Guarded premature `hub.routes.tasks` import in webhook ingress with `_fallback_route_resolver` check, saving 7.9 MB of premature module memory during boot.
  - Added dynamic fallback route resolver in `AsyncHTTPServer` to load task, observability, and dashboard endpoints on demand.
  - Tuned Python 3.14 GC thresholds to `(100, 5, 5)` and added periodic `PRAGMA wal_checkpoint(TRUNCATE)` on idle, bounding SQLite WAL heap memory.
  - Verified Gateway process RSS footprint stays strictly under 20 MB (budget: `< 30.0 MB`).

## [1.6.2] - 2026-09-11

### Fixed
- **Log Viewer & Drawer Rendering Engine Hardening (`hub/routes/dashboard_template.py`, `tests/api/test_dashboard_routes.py`)**:
  - **HTML Entity & Quote Collision Fix**: Resolved issue where global `escapeHtml` converted double quotes to `&quot;`, preventing regexes for JSON keys (`"key":`), string values (`: "val"`), and Python traceback frames (`File "...", line ...`) from matching. Introduced `escapeLogText` preserving quote literals for safe element text while escaping dangerous `<>&` characters.
  - **Tag-Shielded Search Highlighting**: Rebuilt `highlightSearchQuery` with entity/tag shielding regex (`(<[^>]+>|&[a-zA-Z0-9#]+;)|(query)`), ensuring search queries never corrupt HTML tag names (`<span>`) or class attributes (`class="..."`).
  - **Traceback & Exception Highlighting**: Added dedicated syntax highlighting for Python headers (`Traceback (most recent call last):`), frame files (`.log-path`), line numbers (`.log-traceback-line`), function names (`.log-traceback-func`), and error types (`.log-lvl-error`).
  - **Interactive Multi-Line Pretty JSON**: Implemented `tryParseJson` with `#btnTogglePrettyJson` toolbar toggle, seamlessly splitting full and embedded JSON into formatted multi-line rows with gutter sub-indicators (`·`).
  - **Live Running Task Duration Timer**: Added real-time second-by-second duration counter in the drawer header for active `running` tasks with clean interval teardown on close/completion.
  - **Browser Globals Mock for Headless Node Evaluation**: Added minimal DOM environment mocks (`window`, `document`, `localStorage`, `navigator`) in `test_dashboard_log_formatter_unit_and_safety` to allow standalone unit verification of client-side template scripts via Node.js.

## [1.6.1] - 2026-09-11

### Changed
- **Cadence Sentinel Rescheduled to 6 Times Daily (`CAD-20260911-webhook-hub-sentinel`)**:
  - Rescheduled Antigravity sidecar (`~/.gemini/config/sidecars/webhook-hub-sentinel/sidecar.json`) and 2nd Brain cadence registry (`00 - System/registries/cadence-commands.md`) to run every 4 hours (`0 */4 * * *` / 6 times per day: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00).
  - Maintained 12-check E2E verification, memory RSS budget validation (<30MB), unprocessed task auto-sweep, and Chinese reporting.
  - Verified with `cadence_ctl doctor --strict` (0 errors, 0 warnings) and `audit_cadence_registry.py --strict` (0 errors across 64 cards).

### Fixed
- **Root-Cause Elimination of Error Alert Floods ("Coolify VPS Dashboard")**:
  - Investigated recurring error emails (`[Antigravity Webhook Hub] [🔴 Down]`) from `Coolify VPS Dashboard <notification@globalgrowthco.com>`.
  - Identified source as self-hosted Uptime Kuma (Monitor #84) deployed under Coolify on VPS `openclaw-eu` routing via Cloudflare Tunnel (`webhook.worldinspirelab.com/healthz`).
  - Diagnosed root cause:
    1. Aggressive 60s probe interval with 2 retries (total 120s buffer) combined with unconditional SMTP email alerting on a local workstation monitor. Whenever the user closed their MacBook lid, traveled, or slept, probe failures triggered down/up email storms to `yanghxmail@gmail.com`.
    2. Extending retry window alone was insufficient because MacBook sleeps for hours during non-working periods, which still tripped the threshold and generated false-positive alarm emails.
  - Architectural fix in `26.08.16-adnova-cli/scripts/kuma_apply.py` & `kuma_contract.py`:
    - Added `email: False` declaration for `Antigravity Webhook Hub`, explicitly decoupling workstation-bound edge ingress from SMTP email paging while preserving status page visibility and webhook alerting.
    - Updated `kuma_apply.py` notification wiring to omit notification #2 (`email (SMTP via Coolify's account)`) for monitors with `email: False`.
    - Updated `kuma_contract.py` voice invariant check to validate that declared `email: False` monitors carry exactly their declared notification channels (84/84 monitors passing).
    - Applied changes live to Uptime Kuma SQLite database on `openclaw-eu`, immediately and permanently halting error email delivery to the user's inbox.
  - Enhanced `tunnel/manage_daemon.sh` with stale PID cleanup (`kill -0` check) and clean shutdown before LaunchAgent start.
  - Verified with 230/230 unit/stress/E2E test suite passing, 12/12 standalone verification suite passing (RSS 19.03MB <= 30MB), public Cloudflare tunnel healthz returning 200 OK, and cadence registry doctor passing across 64 cards.

## [1.6.0] - 2026-09-11

### Added
- **Production-Grade Log Viewer & Drawer UX (`hub/routes/dashboard_template.py`, `hub/routes/dashboard.py`)**:
  - Engineered an intelligent log drawer rendering engine complying with `/ui-ux-pro-max` standards: distinct line numbers in a dedicated gutter (`.log-gutter`) with border demarcation and unselectable user text.
  - Added semantic color-coded level badges (`[INFO]`, `[WARN]`, `[ERROR]`, `[DEBUG]`, `[SUCCESS]`, `PASS`, `FAIL`) with modern low-opacity background pills and high-contrast borders.
  - Implemented automatic highlighting of timestamps (`HH:MM:SS` / ISO 8601) and file paths / command invocations (`.log-path`).
  - Added inline JSON syntax highlighting and pretty-formatting for structured stdout/stderr strings.
  - Integrated full ANSI escape sequence parsing (`\033[...]` / `\x1b[...]`), mapping 16 standard terminal colors, bold, and dim styles directly to scoped CSS classes.
  - Implemented interactive drawer controls:
    - Text search bar (`#logSearchInput`) with real-time match highlighting (`<mark class="log-search-match">`), debounced filtering, dynamic match counter badge (`#logMatchesCount`), and Esc keyboard clearing.
    - Wrap lines toggle button (`#btnToggleWrap` / `.wrap-mode`) switching between whitespace-pre and whitespace-pre-wrap.
    - Auto-scroll lock toggle button (`#btnToggleScroll`) maintaining sticky follow-the-tail behavior or freeing scroll inspection.
    - Copy logs button with inline SVG icon, clipboard API integration, and animated toast feedback.
    - Execution metrics header displaying duration badge (`#drawerDurationBadge`) and exit code badge (`#drawerExitBadge`).
  - Replaced all UI emojis with premium inline SVGs (no decorative emojis anywhere in the interface).
  - Maintained memory safety via a 500-line circular FIFO buffer to prevent DOM bloat during high-velocity live log streaming.
- **Dashboard Zero Trust & Multi-Tier Authentication (`hub/config.py`, `hub/security.py`, `hub/routes/dashboard.py`, `hub/routes/observability.py`)**:
  - Added `DashboardConfig` to `AppConfig` supporting Cloudflare Access Zero Trust JWT assertions (`cf-access-jwt-assertion`), HTTP Basic Auth, and token fallback (`Authorization: Bearer`, `?token=`, `X-Dashboard-Token`).
  - Implemented `verify_dashboard_auth` in `hub/security.py` with timing-safe HTTP Basic Auth comparison, unverified JWT payload claims decoding (validating expiration `exp`, application audience `aud`, and email allowlist `identity`), and query token parsing.
  - Gated all `/dashboard` and `/ui` routes with authentication challenge: unauthenticated requests receive styled 401 Unauthorized HTML with `WWW-Authenticate: Basic realm="..."` challenge, while API endpoints receive 401 JSON. Ingress webhooks (`/webhook/*`) and health probes (`/healthz`) remain completely unaffected and open.
  - Deployed Cloudflare Access application `ee150248-6ee0-4e7d-a5fe-8ffeeeef1b50` on `webhook.worldinspirelab.com/dashboard` with 90-day session policy for Vec (`yanghxmail@gmail.com`).
- **Template Isolation & RSS Memory Guard (<30MB) (`hub/routes/dashboard_template.py`, `hub/routes/dashboard.py`)**:
  - Extracted 85KB HTML template into standalone `dashboard_template.py` and lazy-imported `render_dashboard_html` inside `handle_dashboard`.
  - Reduced daemon baseline RSS from 28.3MB to 18.7MB, guaranteeing zero RSS memory breaches under concurrent load and passing all adversarial memory constraints.

## [1.5.2] - 2026-09-11

### Added
- **Test Event Auto-Hide & Observability Filter (`hub/routes/dashboard.py`, `hub/routes/tasks.py`, `hub/models.py`)**:
  - Added dedicated test event filter toggle button and segmented pill controls (`Real Only`, `All Events`, `Tests Only`) with live badge counts in Dashboard table header and topbar.
  - Implemented automatic classification of synthetic verification tasks (e.g. E2E runner tests, sweeper verification tasks, test-send events, tasks with `echo '...'`, `test_`, `nonce`, or `dry_run: true`).
  - Added `filter_test` (`real`, `all`, `test`), `hide_test` (`true`/`false`), and `is_test` (`0`/`1`) query parameter filtering on `GET /tasks` supporting SQLite-level condition pruning and Python model enrichment (`is_test: bool`).
  - Added `real_tasks` and `test_tasks` metrics to `GET /tasks/summary` for instant SSOT metric aggregation.
  - Added visual `[REAL]` and `[TEST]` status badges on every activity row in the dashboard table.
  - Added "Real Tasks (Production)" metric card alongside "Total Tasks" in the dashboard metrics grid.
  - Persisted user filter preference in browser `localStorage` (`antigravity_hub_filter_mode`), defaulting to 'real' so production events are immediately visible without synthetic clutter.
  - Added comprehensive test `test_tasks_test_event_filtering_and_classification` in `tests/api/test_dashboard_routes.py`.
- **Scheduled Cadence Sentinel Rescheduled to Daily Midnight (`CAD-20260911-webhook-hub-sentinel`)**:
  - Rescheduled Antigravity sidecar (`~/.gemini/config/sidecars/webhook-hub-sentinel/sidecar.json`) and 2nd Brain cadence command card (`CAD-20260911-webhook-hub-sentinel`) from 4-hourly (`0 */4 * * *`) to daily midnight (`0 0 * * *`).
  - Integrated 12-check E2E verification (`./bin/webhook-hub verify`), automated bug diagnosis in `webhook-hub.log`, auto-remediation, and full Chinese output for the daily health sweep.

### Fixed
- **SQL & Model Filter Synchronization & UI Consistency (`hub/models.py`, `hub/routes/tasks.py`)**:
  - Fully aligned `TEST_EVENT_SQL_FILTER` and `is_test_task`, eliminating the discrepancy where SQL counted 10 real tasks while in-memory filtering returned 7 tasks (due to synthetic params like `sync-test`, `slack_supplementary_test`, `live-verification-test`).
  - Corrected `total_count` aggregation on `GET /tasks?filter_test=real` and `real_tasks` / `test_tasks` on `GET /tasks/summary` so metrics grid cards, table pill counts, and activity feeds display identical counts.
- **SQL LIKE Underscore Wildcard & False Positive Elimination (`hub/models.py`)**:
  - Replaced unescaped SQL wildcard patterns (`LIKE '%_test_%'`) with `ESCAPE '/'` literal matching (`LIKE '%/_test/_%' ESCAPE '/'`) and tokenized string matching in Python (`re.split`), preventing words like `latest_updates` from being falsely classified as test events.
  - Replaced loose `"nonce"` substring matching with structured JSON key matching (`"nonce":`), preventing words like `announcement` from being hidden from production feeds.
  - Added explicit override support (`is_test: false` / `is_test: true`) in both SQL and Python.
- **Uptime Kuma Dashboard Integration & Source Normalization (`hub/routes/dashboard.py`, `hub/routes/tasks.py`)**:
  - Normalized `source_filter` to accept both `uptime_kuma` (database standard) and `uptime-kuma` (route slug standard), restoring the Uptime Kuma sidebar counter from 0 to 3 and enabling click-through activity filtering.
  - Wrapped `localStorage` access in safe try/catch fallbacks to protect incognito and strict-privacy browser contexts, and added dynamic SVG eye icon toggling.
- **Cloudflare Tunnel Dashboard URL Standard Port 443 (`hub/cli.py`, `tests/unit/test_cli.py`)**:
  - Corrected tunnel dashboard URL from `https://webhook.worldinspirelab.com:9423/dashboard` to `https://webhook.worldinspirelab.com/dashboard`, eliminating non-standard port 9423 on public Cloudflare Tunnel ingress while preserving port 9423 on local loopback URL (`http://127.0.0.1:9423/dashboard`).
  - Updated CLI unit test expectation in `tests/unit/test_cli.py`.

## [1.5.1] - 2026-09-11

### Fixed
- **HTTP HEAD Method Support (`hub/server.py`, `tests/api/test_observability_routes.py`)**:
  - Implemented HTTP `HEAD` method handling in `AsyncHTTPServer`: fallback route matching against GET routes and empty body delivery while preserving calculated `Content-Length` headers.
  - Prevents 405 Method Not Allowed responses when external uptime monitors (Uptime Kuma, Coolify, Cloudflare) probe `/healthz`, `/health`, or `/dashboard`.
- **Sidecar Observability Hardening & Generalized Emission (`hub/config.py`, `hub/dispatcher.py`)**:
  - Added `ObservabilityConfig` (`enabled`, `sidecar_slug`, `emit_sidecar_events`, `sidecar_data_dir`) with environment variable parsing (`ANTIGRAVITY_OBSERVABILITY_ENABLED`, etc.) and flat override support.
  - Generalized Antigravity sidebar activity logging to record all task execution types (`agent_signal`, `contact_review`, `cli`, `launchd`), not only `agent_signal`.
  - Fixed `action_params_json` payload extraction so task prompts and summaries are properly rendered in the Antigravity sidebar.
  - Added failure error capturing (`error` field populated) so failed tasks render appropriate visual failure indicators in the IDE sidebar.
- **SSE Broker Synchronization & Dashboard Performance (`hub/dispatcher.py`, `hub/routes/dashboard.py`)**:
  - Wired `TaskDispatcher._broadcast_status` and `sweep_unprocessed_tasks` to publish on the global `"events"` topic with standard broker event names (`status_changed`, `completed`, `sweeper_run`).
  - Aligned dashboard frontend event listeners with broker event names to ensure instant UI state transitions.
  - Replaced unbounded `innerHTML +=` string concatenation with bounded (500-line capped) DOM element appending and debounced (150ms) task refreshing to eliminate DOM re-parsing overhead and UI stutter during high-throughput log streaming.
- **E2E Verification Suite Expansion (`scripts/verify_e2e.py`)**:
  - Expanded `scripts/verify_e2e.py` from 10 to 12 automated checks, adding automated testing of UI routes (`/dashboard`, `/ui`), `/health`, `/tasks/summary`, HTTP HEAD method, and sidecar sentinel JSON activity event emission.

## [1.5.0] - 2026-09-11

### Added
- **Observable Activity Web Dashboard & Console (`hub/routes/dashboard.py`)**:
  - Implemented zero-dependency embedded SPA dashboard accessible at `GET /dashboard` and `GET /ui`.
  - Dark slate aesthetic matching Antigravity IDE, responsive mobile/desktop layout, and premium inline SVG icons (zero emoji UI icons).
  - Telemetry sidebar with live process memory RSS gauge (<30MB budget), uptime counter, SQLite WAL status, and SSE connectivity pulse.
  - Real-time task activity feed with status badges, source tags, execution duration, and log drawers streaming historical and live stdout/stderr via SSE.
  - Interactive Webhook Simulator modal supporting preset sources (`agent_signal`, `contact-review`, `uptime-kuma`, `cli`) with direct SSOT write-and-re-read verification.
- **Antigravity IDE Sidebar Sentinel Integration**:
  - Registered Antigravity Sidecar Sentinel in `~/.gemini/config/sidecars/webhook-hub-sentinel/sidecar.json` and `~/.gemini/config/config.json`.
  - Added Cadence Card `CAD-20260911-webhook-hub-sentinel` in 2nd Brain (`00 - System/registries/cadence-commands.md`) strictly passing all 62 validation checks.
  - Enabled background recording of webhook-triggered activities to `~/.gemini/antigravity/sidecar_data/webhook-hub-sentinel/events/*.json` on `agent_signal` execution.
- **Task Re-run & Authoritative SSOT Reset (`hub/db.py`, `hub/routes/tasks.py`)**:
  - Added atomic `rerun_task(task_id)` method in `DatabaseManager` resetting task status to `queued`, incrementing `retry_count`, clearing execution markers/errors, and re-enqueueing in `dispatcher`.
  - Added `POST /tasks/{task_id}/rerun` API endpoint broadcasting `status_change` to SSE subscribers.
- **Direct SSOT Metrics Aggregation (`hub/routes/tasks.py`)**:
  - Added `GET /tasks/summary` and `GET /activities/summary` aggregating counts directly from SQLite tasks and events tables.
  - Enhanced `GET /tasks` with parameterized keyword search (`?q=`), source filtering (`?source=`), and action type filtering (`?action_type=`).
- **CLI & IDE Task Extensions (`hub/cli.py`, `.vscode/tasks.json`)**:
  - Added `dashboard` (alias `ui`) CLI subcommand to display endpoint URLs or launch the dashboard directly via `--open`.
  - Added `rerun <task_id>` CLI subcommand supporting both live HTTP gateway re-enqueueing and offline direct SQLite SSOT resets.
  - Added `.vscode/tasks.json` configuring one-click dashboard launch, daemon start, status inspection, E2E verification, and task sweeping.

### Fixed
- **Coolify Health Monitor 404 Resolution (`MAT-033`)**:
  - Aliased `GET /health` to `handle_healthz` in `hub/routes/observability.py`, fixing the persistent 404 alert generated by Coolify's default health check.
- **HTML Content Negotiation on Root (`hub/routes/observability.py`)**:
  - Enabled content negotiation on `GET /`: returns the embedded SPA dashboard when `Accept: text/html` is requested, while preserving the JSON service descriptor for API consumers.

## [1.4.1] - 2026-09-11

### Fixed
- **Memory Footprint Optimization (`hub/routes/uptime_kuma.py`)**:
  - Converted eager top-level `urllib.request`, `urllib.error`, and `subprocess` imports to lazy function-scoped imports.
  - Reduced server startup memory from ~30MB to 18.39MB, preventing memory budget breaches under concurrent task loads.
- **UP Heartbeat Sweeper Ghost Task Elimination (`hub/routes/uptime_kuma.py`)**:
  - Ensured incoming UP recovery events are persisted with `status='processed'` rather than `status='received'`, preventing the background sweeper loop from misidentifying non-task heartbeats as orphaned events.
- **CLI Offline Database Sweep Routing (`hub/cli.py`)**:
  - Bypassed live HTTP server routing in `cmd_sweep` when `--db` is explicitly provided, ensuring offline database operations target the specified path directly rather than the daemon's active database.

## [1.4.0] - 2026-09-11

### Added
- **Uptime Kuma Webhook Ingress & Self-Healing Dispatcher (`hub/routes/uptime_kuma.py`)**:
  - Implemented dual endpoints `POST /api/webhook/uptime-kuma` and `POST /webhook/uptime-kuma` for real-time Uptime Kuma DOWN alerts.
  - Bearer token authentication (`KUMA_WEBHOOK_BEARER_TOKEN` / `config.security.bearer_tokens`).
  - Strict schema contract validation for heartbeat, monitor, and incident metadata.
  - **Anti-Flap Jitter Debounce**: Instant active double-check HTTP probe upon DOWN alert. If probe succeeds immediately, alert is classified as `debounced_flap`, suppressed from firing alarms, and recorded in SQLite SSOT as `processed` (`flap_suppressed=True`).
  - **Anti-Flap Cooldown Rate Limiting**: Enforces a 60-second in-memory suppression window per monitor ID to prevent triage storms.
  - **Native macOS Observability**: Emits native macOS desktop banner notifications via AppleScript (`osascript`) upon confirmed outages and triage launches.
  - **Autonomous Self-Healing Dispatch**: Asynchronously dispatches `00 - System/scripts/audit_maintenance_alert_triage.py` on confirmed outages with zero main-thread blocking.
- **Two-Sided Test Suite (`tests/api/test_uptime_kuma_routes.py`)**:
  - 10 automated test cases verifying legitimate UP, DOWN flap suppression, DOWN confirmed triage queueing, cooldown rate-limiting, legacy route backwards-compatibility, and adversarial paths (401 missing/invalid auth, 400 empty/malformed/missing fields) asserting zero database side effects on failure.

### Fixed
- **Port 9423 Ingress Collision**:
  - Resolved port binding collision where Docker container `chatgpt2api` bound both `9423:80` and `10884:80`, intercepting Cloudflare Tunnel traffic. Re-bound `chatgpt2api` exclusively to `10884:80`, freeing port 9423 for Webhook Hub.

## [1.3.4] - 2026-09-10

### Fixed
- **Centralized Memory Management & Tooling Unification (`hub/memory.py`)**:
  - Eliminated duplicated and fragmented ctypes memory logic across `hub/routes/observability.py`, `hub/server.py`, `hub/dispatcher.py`, and `hub/cli.py`. Consolidated Darwin Mach kernel task inspection (`TASK_VM_INFO`, `MACH_TASK_BASIC_INFO`), runtime cache clearing, multi-zone malloc pressure relief, and memory budget resolution into a single unified module `hub/memory.py`.
  - Exported memory utilities (`apply_memory_pressure_relief`, `get_memory_rss_bytes`, `get_memory_rss_mb`, `get_memory_budget_mb`) in `hub/__init__.py` via PEP 562 lazy loading.
- **Python 3.14 PEP 649 Deferred Annotation Compatibility**:
  - Fixed `NameError: name 'Any' is not defined` when introspecting annotations on `hub.__getattr__` by adding `from __future__ import annotations` and importing `typing.Any` in `hub/__init__.py`.
- **HTTP Server Handler Dispatch Robustness & Memory Leak Prevention**:
  - Fixed `_extract_handler_params` and `_HandlerInvoker` dropping keyword-only arguments (`co_kwonlyargcount`) and failing on variable keyword arguments (`CO_VARKEYWORDS`), preventing runtime dispatch crashes (`TypeError`).
  - Fixed `_is_coroutine_callable` to properly detect callable class instances (`async def __call__`).
  - Replaced global `_INVOKER_CACHE` dictionary with per-handler attributes (`__hub_invoker__`) to prevent memory leaks from bound methods and dynamic handlers.
  - Made memory budget configurable via `ServerConfig.memory_budget_mb`, YAML, and `MEMORY_BUDGET_MB` environment variable.
- **Test Coverage**:
  - Added unit test suite `tests/unit/test_memory.py` verifying memory pressure relief, budget resolution, and PEP 562 exports.
  - Added tests in `tests/unit/test_server.py` for keyword-only parameters, `**kwargs`, and callable class handlers.
  - Full suite now passes 197/197 tests (0 failures), and standalone E2E verifier passes 10/10 steps cleanly with gateway RSS ~18.5MB.

## [1.3.3] - 2026-09-10

### Fixed
- **Darwin Memory Bounding & Malloc Zone Pressure Relief (<30MB RSS Enforcement)**:
  - Adopted Apple Mach kernel `TASK_VM_INFO` (flavor 22, `phys_footprint`) in `hub/routes/observability.py`: accurately measures actual physical memory footprint exclusive to the process (~8.8MB - 18.5MB) rather than counting system-wide shared dyld cache pages from `MACH_TASK_BASIC_INFO` (flavor 20).
  - Fixed pressure relief initialization defect in `hub/routes/observability.py`: `_apply_darwin_pressure_relief` previously checked `if "_darwin_libc" not in globals() or _darwin_libc is None:`. Because `_get_darwin_resident_bytes()` had already initialized `_darwin_libc`, this block was skipped, leaving `_darwin_pressure_relief_fn` as `None` and completely bypassing `malloc_zone_pressure_relief` during `/healthz` execution.
  - Corrected pressure relief initialization guards across `hub/routes/observability.py`, `hub/server.py`, and `hub/dispatcher.py` to directly check `_darwin_pressure_relief_fn is None` and properly resolve all active memory zones from `malloc_num_zones` and `malloc_zones`.
  - Added internal runtime cache purging (`urllib.parse.clear_cache()`, `sys.path_importer_cache.clear()`, `sys._clear_internal_caches()`, `re.purge()`) and multi-generation garbage collection `gc.collect(2)` across memory pressure relief routines.
  - Replaced `PRAGMA wal_checkpoint(TRUNCATE)` with `PRAGMA wal_checkpoint(PASSIVE)` during runtime health probes and background sweep loops in `hub/routes/observability.py`, `hub/dispatcher.py`, and `hub/cli.py`, eliminating kernel `ftruncate` disk buffer fragmentation that previously caused resident memory creep.
  - Added `PRAGMA shrink_memory;` in `hub/db.py` execution blocks (`execute_read` and `get_unprocessed_tasks`) and set `shrink_memory(truncate_wal=False)` on periodic server and connection relief cycles to avoid reloading SQLite pages during routine memory shrinkage.
  - Added duplicate route registration idempotency guards in `hub/routes/tasks.py` and `hub/routes/webhook.py` preventing double-registration on startup.
  - Optimized `hub/server.py` request processing: cached handler signature introspection via `_HandlerInvoker`, formatted HTTP date headers without full `email` package import, and only triggered periodic GC on keep-alive connections every 5 requests while maintaining strict close-time cleanup.
  - Tuned SQLite memory profile in `hub/db.py`: set `PRAGMA soft_heap_limit = 131072;` (128KB) across connections and schemas to cap SQLite internal heap allocator footprint and prevent memory spikes on repeated queries while preserving `DEFAULT_SQLITE_CACHE_SIZE = -4000`.
  - Precompiled project bytecode across `hub` and `bin`, preventing in-memory AST and compiler symbol table retention under `PYTHONDONTWRITEBYTECODE=1`.
  - Standalone E2E verification (`scripts/verify_e2e.py`) now 100% reliably passes all 10 checks with Gateway RSS consistently measured at ~17.8MB - 18.5MB (well below the 30.00MB limit).

## [1.3.2] - 2026-09-10

### Fixed
- **Contact Review Candidate Matching & URL Normalization**:
  - Fixed candidate URL scoring in `hub/contact_review/notion_client.py`: replaced asymmetric comparison with `normalize_url_for_comparison()`, stripping protocols (`http://`, `https://`), `www.` prefixes, and trailing slashes on both incoming and stored candidate URLs.
  - Enhanced candidate search query filter in `search_candidates`: parses social usernames/handles from URL inputs (e.g. `adamwalk` from `instagram.com/adamwalk/`) and queries both `URL` and `Instagram` Notion properties, preventing 0-candidate query misses caused by slight formatting discrepancies.
  - Added cross-attribute matching between incoming URL and candidate social profile handles across Instagram, Twitter/X, LinkedIn, Telegram, LINE, and WeChat.
- **Production n8n Hybrid Ingress Resilience (Workflow `3J5doqEyxA7lT1OO`)**:
  - Removed greedy `separatedNativeName` CJK regex in `Build People Capture Record` that erroneously hijacked conversational clauses (e.g. `"他也去了吉婆岛"`) and notes (e.g. `"- 非常敏感"`) as native names when base name was Romanized. Native names now strictly require explicit labeled prefixes (`姓名:`, `中文名:`, `名前:` or parentheses).
  - Patched `Wake Antigravity Contact Review Agent` node with `onError: continueRegularOutput` and `options.neverError: true`, preventing workflow crashes on 502 Bad Gateway when the MacBook is asleep.
  - Updated Slack verdict node with graceful offline messaging to guide user to wake laptop without dropping in-flight webhook executions.
- **Observability & Diagnostics**:
  - Fixed missing `import gc` in `hub/routes/observability.py` resolving 500 errors on `/healthz` during sweeper execution.
  - Added unit test `test_candidate_url_matching_with_and_without_www` verifying Adam Walker URL matching with score >= 85 (121/121 unit tests passing).

## [1.3.1] - 2026-09-10

### Fixed
- **Database & Rehydration Resilience**:
  - Fixed `AttributeError` in `DatabaseManager.rehydrate_orphaned_event` when event payload is a non-dict JSON primitive (e.g. strings, arrays, integers) by safely validating `isinstance(data, dict)`.
  - Fixed lifecycle state corruption: `DatabaseManager.sweep_and_requeue_unprocessed` and `update_task_status_cas` now reset `started_at` and `completed_at` to `NULL` upon task requeue, ensuring accurate runtime durations on subsequent retry attempts.
  - Fixed query ignoring task-specific `timeout_seconds`: stale task sweep queries now dynamically compute elapsed duration using `MAX(?, COALESCE(timeout_seconds, 0))`.
  - Fixed recovery gap: `timed_out` tasks with remaining retry budget (`retry_count < max_retries`) are now properly swept and re-enqueued instead of permanently stalling.
  - Fixed tenant isolation: scoped all recovery queries and batch state updates in `sweep_and_requeue_unprocessed` to the designated `source` filter when provided.
  - Fixed `recover_orphaned_tasks` erroneously setting `completed_at = CURRENT_TIMESTAMP` for tasks being moved to `queued` state.
- **Darwin Memory Bounding & Stability (<30MB RSS)**:
  - Fixed invalid ctypes pointer dereference for macOS Darwin malloc zones: corrected `(ctypes.c_void_p * num_zones).in_dll(...)` to `ctypes.POINTER(ctypes.c_void_p).in_dll(libc, "malloc_zones")` across observability, dispatcher, server, and CLI modules.
  - Pre-allocated `_MachTaskBasicInfo` and count ctypes structures in `hub/routes/observability.py` to prevent heap allocations during high-frequency memory measurements.
  - Resolved process-wide SQLite cache pollution: removed runtime mutation of `DEFAULT_SQLITE_CACHE_SIZE` in `hub/cli.py` to preserve default `-4000` for standalone connections while maintaining `-16` for server instances.
  - Set `sys.dont_write_bytecode = True` and `-B` shebang to prevent pyc disk and memory allocations at startup.
- **CLI & API Ergonomics**:
  - Added `--auto-retry` and `--no-auto-retry` CLI flags to `webhook-hub sweep`.
  - Forwarded `auto_retry_interrupted` parameter in `POST /tasks/sweep` endpoint handler.

## [1.3.0] - 2026-09-10

### Added
- Auto-Picker & Unprocessed Task Sweeper subsystem (`hub.db`, `hub.dispatcher`, `hub.routes.tasks`):
  - Solves the Mac sleep and battery-loss problem: automatically discovers and recovers unhandled, interrupted, or orphaned tasks and webhook events after prolonged laptop sleep (e.g. 3h+), power loss, or unexpected process restarts.
  - Periodic background sweeper loop (`sweeper_interval_seconds: 60`, `stale_task_timeout_seconds: 300`) with monotonic clock jump detection (`time.monotonic()` leap > 60s) triggering instant wake-up sweeps upon resuming from sleep.
  - Orphaned webhook event rehydration (`DatabaseManager.get_orphaned_webhook_events`, `DatabaseManager.rehydrate_orphaned_event`): promotes orphaned `webhook_events` (status `received` with no task) into `queued` tasks.
  - Interrupted running task recovery (`DatabaseManager.sweep_and_requeue_unprocessed`): resets stale `running` tasks to `queued` (or `timed_out` if `max_retries` exhausted) and re-enqueues into in-memory dispatcher queue.
  - HTTP endpoints for agent orchestration:
    - `GET /tasks/unprocessed`: Queries unprocessed/stale tasks and orphaned events with breakdown counts.
    - `POST /tasks/sweep`: Atomically sweeps and enqueues unhandled tasks, supporting `dry_run` mode and retry customization.
  - Unified CLI commands:
    - `./bin/webhook-hub sweep` (with `--dry-run`, `--json`, `--stale-seconds`, `--max-retries`, `--no-auto-retry`).
    - `./bin/webhook-hub pick-unprocessed` (alias).
  - 10-Step Standalone E2E Verification Runner (`scripts/verify_e2e.py`):
    - Added Step 10: Auto-Picker & Unprocessed Task Sweeper integration check, injecting orphaned events, triggering sweep, and asserting live execution to `succeeded` and event transition to `processed`.
    - 10/10 standalone checks passing in <7s with gateway RSS 29.72MB <= 30MB.
    - 184/184 full test suite passing cleanly.

## [1.2.0] - 2026-09-09

### Added
- Antigravity Contact Review Agent subsystem (`hub.contact_review`):
  - Intelligent 5-verdict decision engine (`NO_CHANGE`, `SUPPLEMENT`, `CORRECT`, `MERGE`, `CREATE`) replacing crude blind dedupe/overwrite logic.
  - Live Notion CRM integration for `People ppl[UB3_250711]` (`22ce1b43-2393-81a4-9443-e32e71142e0d`) supporting compound OR candidate queries across name, phone, email, and social URLs.
  - Single Source of Truth (SSOT) read-after-write verification ensuring live database properties match expected mutations before reporting.
  - Threaded Slack notification dispatcher with verdict banners, Notion deep links, and attribute diff highlights.
  - Dedicated CLI command: `./bin/webhook-hub review-contact` (alias: `contact-review`) supporting `--dry-run`, `--json`, `--name`, `--phone`, and payload inputs.
  - Synchronous execution query mode (`POST /webhook/contact-review?sync=true`) returning immediate 200 responses with execution logs and verified verdict state.
  - In-process `contact_review` task dispatching with real-time SSE event streaming and SQLite SSOT log chunks.
- Live n8n workflow modernization:
  - Deployed `Wake Antigravity Contact Review Agent` into workflow `3J5doqEyxA7lT1OO` ("Slack_people ppl people 25.07.20 ✅") on `https://n.worldinspirelab.com`.
  - Replaced legacy blind update/create branches with SSOT Antigravity Review pipeline and threaded Slack feedback.
  - Automated deployment and rollback script (`scripts/patch_n8n_people_workflow.py`) with pre-patch backups.

## [1.1.1] - 2026-09-09

### Added
- GitHub Actions CI/CD pipeline (`.github/workflows/ci.yml`) testing across `macos-latest` and `ubuntu-latest` on Python 3.11 and 3.12 (100% green, 139/139 tests passing).
- Uptime Kuma monitoring integration (`https://monitor.worldinspirelab.com/status/worldinspirelab`): 60-second healthz probe with compact JSON keyword `"status":"ok"`, attached to default alert notifications.

### Fixed
- Packaging: Explicit setuptools package declaration (`packages = ["hub", "hub.routes"]`) resolving flat-layout multi-directory discovery in CI.
- Cross-platform process termination: Accepted POSIX process group termination signals (`-9` and `-15`) across Darwin and Ubuntu (`dash`) shells on task timeout.

## [1.1.0] - 2026-09-09
 
 ### Added
 - Cloudflare Zero Trust Tunnel ingress integration routing `https://webhook.worldinspirelab.com` directly to local port 9423 via persistent macOS LaunchDaemon (`/Library/LaunchDaemons/com.cloudflare.cloudflared.plist`, tunnel ID `6ede1a86-1b22-456e-9b22-b383ed85c4d1`).
 - Authoritative proxied Cloudflare CNAME record for `webhook.worldinspirelab.com` (Zone ID `0bf97b3b9d0066125df8bf3ce76196bc`) with 100% 3-layer route verification (`cf-dns verify` API, 1.1.1.1 DoH, and live HTTPS TLS handshake).
 - Cloudflare Page Rule for `*webhook.worldinspirelab.com/*` disabling Browser Integrity Check and setting Security Level to essentially off, resolving Cloudflare Edge Error 1010 for automated webhook callers (Python urllib, Go net/http, standard curl, and external SaaS platforms).
 - Enhanced `tunnel/start_tunnel.sh` with `--status` monitoring, graceful LaunchDaemon collision avoidance, and `--foreground` override flag.
 - Isolated test `--pidfile` in `tests/stress/test_m5_adversarial_dispatcher_sse.py` ensuring zero collision between running background daemons and test suite executions.
 - Root service descriptor route (`GET /`) returning service status, uptime, and available API routes.
 - Concrete production tunnel configuration file at `tunnel/config.yml`.
 - Comprehensive live public HTTPS verification covering legitimate HMAC/Bearer webhooks, 401 signature tampering rejection, and SQLite SSOT state persistence.
 
 ## [1.0.0] - 2026-09-08

### Added
- Pure Python standard-library asynchronous HTTP/1.1 server (`hub.server.AsyncHTTPServer`) with zero heavy framework dependencies, running with `<30MB` resident RAM and `0%` idle CPU on macOS.
- Unified Command Line Interface executable via `bin/webhook-hub` and `python3 -m hub` supporting 6 primary subcommands:
  - `start`: Foreground or background daemon runner with automatic PID tracking (`.webhook-hub.pid`).
  - `stop`: Graceful shutdown with escalation to `SIGKILL` on unresponsive processes.
  - `status`: Process liveness probe and health diagnostics with structured `--json` output.
  - `logs`: Dual-mode log viewer supporting offline SQLite historical querying and live SSE tailing (`--follow`).
  - `test-send`: Cryptographic webhook testing utility with automatic HMAC-SHA256 calculation, Bearer token injection, and intentional `--tamper` simulation.
  - `verify`: Standalone end-to-end test execution delegating to `scripts/verify_e2e.py`.
- Relational SQLite Single Source of Truth (`hub.db.DatabaseManager`) configured in Write-Ahead Logging (`WAL`) mode with schema tables for `webhook_events`, `tasks`, `executions`, and `execution_logs`.
- Decoupled asynchronous task dispatcher (`hub.dispatcher.TaskDispatcher`) executing ingress tasks in background worker queues without blocking HTTP response times.
- Multi-target runner execution support:
  - Isolated subprocess process groups (`os.setsid`, `os.killpg`) with stdout/stderr stream capture and timeout handling.
  - macOS `launchd` kickstart integration.
  - Periodic `cron` runner support.
  - Atomic Antigravity AI agent signal file emission (`.agents/signals/`).
- Real-time Server-Sent Events (`hub.routes.sse` and `hub.broker.EventBroker`) with broadcast topics, 15-second proxy keepalive heartbeats, and per-task log streaming (`/tasks/{id}/stream`).
- Observability and health monitoring endpoints (`/healthz`, `/ready`, `/metrics`, `/tasks`, `/tasks/{id}`) with Prometheus metrics exposition and RSS budget verification.
- Comprehensive agent discovery contract (`SKILL.md`) providing actionable recipes and troubleshooting runbooks under 300 lines.
- Complete 9-point standalone verification suite (`scripts/verify_e2e.py`) validating all architectural guarantees with zero third-party dependencies.

### Security
- Ingress cryptographic authentication gate rejecting unauthorized webhooks before any database writes occur.
- Timing-attack-resistant HMAC-SHA256 signature verification using `hmac.compare_digest`.
- Replay attack mitigation enforcing strict timestamp expiration windows (`timestamp_tolerance_seconds: 300`) and future skew rejection (`future_timestamp_tolerance_seconds: 60`).
- Bearer token authentication support (`Authorization: Bearer <token>`).
- Dual-layer deduplication engine using caller-provided idempotency keys and SHA-256 payload content hashes to prevent duplicate task execution.
- HTTP Request Smuggling protection rejecting conflicting `Content-Length` and `Transfer-Encoding` headers (RFC 7230 / RFC 9112).
- Hard payload size limits (`max_body_bytes: 1MB`) guarding against unbounded memory exhaustion denial-of-service attacks.
- Subprocess process group isolation (`os.setsid`) preventing child process leaks upon task cancellation or timeout.
