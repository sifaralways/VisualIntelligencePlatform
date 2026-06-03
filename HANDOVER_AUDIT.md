# Frontend Component Inventory

Static analysis scope: files under [frontend/src/components](frontend/src/components) and [frontend/src/pages](frontend/src/pages), plus import/usage checks across [frontend/src](frontend/src).
Uncertainty: this is static usage only; no runtime feature-flag/dynamic-import checks.

| Component | What it does (one line) | Reuse | Overlap elsewhere |
|---|---|---|---|
| [AnalysisPanel](frontend/src/components/AnalysisPanel.tsx#L41) | Analysis doc viewer/editor for one photo (labels, amendments, rebuild, single-file writeback). | Reused via [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L9). | Some overlap with tag editing UX in [PhotoDetail](frontend/src/components/PhotoDetail.tsx). |
| [FaceCard](frontend/src/components/AnalysisPanel.tsx#L288) | Renders a face block inside analysis details. | File-local only. | Face card/tile concepts also in [PeoplePage](frontend/src/pages/PeoplePage.tsx#L2690) and [AssistantPage](frontend/src/pages/AssistantPage.tsx#L419). |
| [LabelChip](frontend/src/components/AnalysisPanel.tsx#L377) | Editable label chip with rename/delete/undo controls. | File-local only. | Conceptually overlaps with tag chip UI in [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L883). |
| [Section (AnalysisPanel)](frontend/src/components/AnalysisPanel.tsx#L452) | Section wrapper for grouped analysis content. | File-local only. | Similar section wrappers exist in [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L872). |
| [Attr](frontend/src/components/AnalysisPanel.tsx#L468) | Small label-value row in analysis UI. | File-local only. | Similar metadata rows appear inline in [PhotoDetail](frontend/src/components/PhotoDetail.tsx). |
| [IconBtn](frontend/src/components/AnalysisPanel.tsx#L477) | Small icon-only button helper. | File-local only. | Repeated icon button styling appears inline across many pages. |
| [HistorySection](frontend/src/components/AnalysisPanel.tsx#L497) | Shows amendment history timeline/list. | File-local only. | No direct shared equivalent. |
| [ConnectionsGraph](frontend/src/components/ConnectionsGraph.tsx#L238) | Interactive people/cluster graph with depth/filter/layout/merge actions. | Reused in [PeoplePage](frontend/src/pages/PeoplePage.tsx#L12) and [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L10). | No direct duplicate. |
| [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L48) | Main photo detail modal with metadata, faces, tags, analysis tab. | Reused in [App](frontend/src/App.tsx#L15), [SearchPage](frontend/src/pages/SearchPage.tsx#L8), [AssistantPage](frontend/src/pages/AssistantPage.tsx#L11), [PeoplePage](frontend/src/pages/PeoplePage.tsx#L13). | No direct duplicate. |
| [Section (PhotoDetail)](frontend/src/components/PhotoDetail.tsx#L872) | Section container for detail modal subsections. | File-local only. | Similar to [Section (AnalysisPanel)](frontend/src/components/AnalysisPanel.tsx#L452). |
| [TagChips](frontend/src/components/PhotoDetail.tsx#L883) | Tag-chip list with optional remove action. | File-local only. | Similar interaction style to [LabelChip](frontend/src/components/AnalysisPanel.tsx#L377). |
| [PhotoGrid](frontend/src/components/PhotoGrid.tsx#L27) | Paginated/selectable media grid with optional batch actions. | Reused in [LibraryPage](frontend/src/pages/LibraryPage.tsx#L10) and [App](frontend/src/App.tsx#L13). | Grid/tile rendering overlaps with [SearchTile](frontend/src/pages/SearchPage.tsx#L159) and [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483). |
| [PhotoTile](frontend/src/components/PhotoGrid.tsx#L353) | Single tile renderer inside PhotoGrid. | File-local only. | Similar tile card logic in [SearchTile](frontend/src/pages/SearchPage.tsx#L159) and [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483). |
| [PipelinePanel](frontend/src/components/PipelinePanel.tsx#L23) | Sidebar panel for pipeline controls/status/live log with websocket stream. | Reused once in shell [App](frontend/src/App.tsx#L14). | Functionally overlaps with [PipelinePage](frontend/src/pages/PipelinePage.tsx#L9). |
| [Pill](frontend/src/components/RemoteServersPanel.tsx#L34) | Status pill for remote server checks. | File-local only. | No shared status badge component elsewhere. |
| [Field](frontend/src/components/RemoteServersPanel.tsx#L48) | Labeled form field wrapper for wizard inputs. | File-local only. | Similar labeled form groups in admin/settings areas. |
| [ServerCard](frontend/src/components/RemoteServersPanel.tsx#L71) | Card view for a saved remote server with actions. | File-local only. | Similar card layouts in [AdminPage](frontend/src/pages/AdminPage.tsx#L240). |
| [Wizard](frontend/src/components/RemoteServersPanel.tsx#L218) | Multi-step remote setup wizard UI. | File-local only. | No direct equivalent. |
| [RemoteServersPanel](frontend/src/components/RemoteServersPanel.tsx#L629) | Remote server CRUD + wizard orchestration. | Reused in [AdminPage](frontend/src/pages/AdminPage.tsx#L18). | No direct duplicate. |
| [AdminPage](frontend/src/pages/AdminPage.tsx#L521) | Admin console: settings, reset operations, manual pilot, contacts match, remote panel. | One-off route page (used in [App](frontend/src/App.tsx#L1501)). | Some stat display overlap with [DashboardPage](frontend/src/pages/DashboardPage.tsx#L14). |
| [TabButton](frontend/src/pages/AdminPage.tsx#L182) | Admin tab switch button. | File-local only. | General button pattern duplicated across pages. |
| [StatCard (Admin)](frontend/src/pages/AdminPage.tsx#L197) | KPI/stat card for admin metrics. | File-local only. | Overlaps with [StatCard (Dashboard)](frontend/src/pages/DashboardPage.tsx#L5). |
| [SimilarityBadge](frontend/src/pages/AdminPage.tsx#L227) | Visual similarity percentage badge. | File-local only. | No shared badge system. |
| [SuggestionCard](frontend/src/pages/AdminPage.tsx#L240) | Card for contacts-match suggestions with accept/reject actions. | File-local only. | Similar suggestion cards/modals in [PeoplePage](frontend/src/pages/PeoplePage.tsx). |
| [ContactsMatchPanel](frontend/src/pages/AdminPage.tsx#L302) | Contacts-to-cluster matching workflow. | File-local only. | Overlaps with people suggestion/review flows in [PeoplePage](frontend/src/pages/PeoplePage.tsx). |
| [AssistantPage](frontend/src/pages/AssistantPage.tsx#L72) | Chat/assistant UI with result rendering and optional face naming flow. | One-off route page (used in [App](frontend/src/App.tsx#L1222) and [App](frontend/src/App.tsx#L1235)). | Result tile overlap with [SearchPage](frontend/src/pages/SearchPage.tsx#L26). |
| [AssistantFaceTile](frontend/src/pages/AssistantPage.tsx#L419) | Face-result card for assistant unresolved faces. | File-local only. | Similar face tiles in [PeoplePage](frontend/src/pages/PeoplePage.tsx#L2690). |
| [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483) | Media result tile card in assistant results. | File-local only. | Strong overlap with [SearchTile](frontend/src/pages/SearchPage.tsx#L159). |
| [DashboardPage](frontend/src/pages/DashboardPage.tsx#L14) | Top-level operational stats dashboard. | One-off route page (used in [App](frontend/src/App.tsx#L1157)). | Overlaps with admin stats panel in [AdminPage](frontend/src/pages/AdminPage.tsx#L521). |
| [StatCard (Dashboard)](frontend/src/pages/DashboardPage.tsx#L5) | Simple metric card for dashboard. | File-local only. | Overlaps with [StatCard (Admin)](frontend/src/pages/AdminPage.tsx#L197). |
| [DiscoverPage](frontend/src/pages/DiscoverPage.tsx#L27) | Top-tag discovery page by category (animals/places/things). | One-off route page reused with different props in [App](frontend/src/App.tsx#L1176), [App](frontend/src/App.tsx#L1186), [App](frontend/src/App.tsx#L1196). | Overlaps with [TagsPage](frontend/src/pages/TagsPage.tsx#L28). |
| [ExplicitPage](frontend/src/pages/ExplicitPage.tsx#L29) | Explicit-content browsing/filter page. | One-off route page (used in [App](frontend/src/App.tsx#L1206)). | Structure overlaps with [QualityPage](frontend/src/pages/QualityPage.tsx#L13). |
| [LibraryPage](frontend/src/pages/LibraryPage.tsx#L17) | Library/folder scan entry page with PhotoGrid integration. | One-off route page (used in [App](frontend/src/App.tsx#L1160)). | Some folder controls overlap with shell logic in [App](frontend/src/App.tsx). |
| [PeoplePage](frontend/src/pages/PeoplePage.tsx#L22) | Main people/clusters management page: naming, merges, review, suggestions, ignored, graph. | One-off route page (used in [App](frontend/src/App.tsx#L1164)). | Overlaps with people tooling in [PhotoDetail](frontend/src/components/PhotoDetail.tsx) and [ConnectionsGraph](frontend/src/components/ConnectionsGraph.tsx). |
| [ClusterTile](frontend/src/pages/PeoplePage.tsx#L2690) | Tile renderer for unnamed cluster actions/review. | File-local only. | Similar media/face tile UX in [PhotoGrid](frontend/src/components/PhotoGrid.tsx#L353). |
| [PipelinePage](frontend/src/pages/PipelinePage.tsx#L9) | Full-page pipeline control/monitoring UI. | Appears unused in current routing/import graph. | Overlaps with [PipelinePanel](frontend/src/components/PipelinePanel.tsx#L23). |
| [QualityPage](frontend/src/pages/QualityPage.tsx#L13) | Quality issue browser with batch select/delete and confirm modal. | One-off route page (used in [App](frontend/src/App.tsx#L1253)). | Layout overlaps with [ExplicitPage](frontend/src/pages/ExplicitPage.tsx#L29). |
| [SearchPage](frontend/src/pages/SearchPage.tsx#L26) | Natural/classic search page with photo-result tiles and detail modal. | One-off route page (used in [App](frontend/src/App.tsx#L1218)). | Result tiles overlap with [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483). |
| [SearchTile](frontend/src/pages/SearchPage.tsx#L159) | Single search result tile card. | File-local only. | Strong overlap with [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483). |
| [TagsPage](frontend/src/pages/TagsPage.tsx#L28) | Global tag browser across categories with filtering. | One-off route page (used in [App](frontend/src/App.tsx#L1247)). | Overlaps with [DiscoverPage](frontend/src/pages/DiscoverPage.tsx#L27). |
| [WritebackPage](frontend/src/pages/WritebackPage.tsx#L12) | Writeback queue preview/confirm/retry UI. | One-off route page (used in [App](frontend/src/App.tsx#L1250)). | No direct duplicate. |

# Reusable Patterns Actually Used

Shared via components:
- Photo grid and selection behavior is shared via [PhotoGrid](frontend/src/components/PhotoGrid.tsx#L27).
- Rich photo detail interactions are centralized in [PhotoDetail](frontend/src/components/PhotoDetail.tsx#L48).
- Relationship graph is shared via [ConnectionsGraph](frontend/src/components/ConnectionsGraph.tsx#L238).
- Pipeline sidebar is shared via [PipelinePanel](frontend/src/components/PipelinePanel.tsx#L23).

Re-implemented inline (inconsistent):
- Modals/dialogs are repeatedly hand-rolled using fixed overlay markup in [App](frontend/src/App.tsx#L1507), [PeoplePage](frontend/src/pages/PeoplePage.tsx#L1099), [QualityPage](frontend/src/pages/QualityPage.tsx#L194), [AdminPage](frontend/src/pages/AdminPage.tsx#L1108).
- Loading states are mostly per-page booleans and inline text in [DashboardPage](frontend/src/pages/DashboardPage.tsx#L43), [QualityPage](frontend/src/pages/QualityPage.tsx#L117), [AdminPage](frontend/src/pages/AdminPage.tsx#L723), [App](frontend/src/App.tsx#L955).
- Empty states are also inline per view rather than shared components in [PhotoGrid](frontend/src/components/PhotoGrid.tsx), [PeoplePage](frontend/src/pages/PeoplePage.tsx), [WritebackPage](frontend/src/pages/WritebackPage.tsx).
- Button styling is largely repeated utility-class strings across most pages; no shared Button primitive.
- Card/stat UI has duplication: [StatCard (Admin)](frontend/src/pages/AdminPage.tsx#L197) vs [StatCard (Dashboard)](frontend/src/pages/DashboardPage.tsx#L5).
- Tile/card duplication exists between [SearchTile](frontend/src/pages/SearchPage.tsx#L159) and [AssistantTile](frontend/src/pages/AssistantPage.tsx#L483).

Honest inconsistency verdict:
- The codebase has strong shared domain components for complex workflows, but foundational UI primitives (Modal, Button, EmptyState, LoadingState, Toast) are not centralized.

# API Client Patterns

Coverage check:
- [frontend/src/api/client.ts](frontend/src/api/client.ts) is the primary HTTP client abstraction and is widely used across pages/components.
- Direct HTTP fetch/axios outside client file: none found.
- Direct websocket use outside client file: present in [App](frontend/src/App.tsx#L678), [PipelinePanel](frontend/src/components/PipelinePanel.tsx#L53), and [PipelinePage](frontend/src/pages/PipelinePage.tsx#L23).

Endpoint coverage vs backend routes:
- Most backend route families appear wrapped (profiles, media, folders, persons/clusters/faces, search, chat, writeback, tags, analysis, admin, settings, remote).
- One backend endpoint appears uncovered by client wrapper: [POST /api/pipeline/rebuild_clip_index](backend/api/routes/pipeline.py#L714).
- Note: [PipelinePage](frontend/src/pages/PipelinePage.tsx#L9) exists but is not currently routed/used, so websocket logic there may be stale duplication.

# Backend Service Patterns

DB query patterns:
- Shared pattern is consistent: route handlers generally use async db context from [backend/database/db.py](backend/database/db.py) via `async with get_db()` in route files such as [media.py](backend/api/routes/media.py), [persons.py](backend/api/routes/persons.py), [folders.py](backend/api/routes/folders.py).
- SQL itself is mostly inline per-route (large SQL strings in [persons.py](backend/api/routes/persons.py), [search.py](backend/api/routes/search.py), [admin.py](backend/api/routes/admin.py), [folders.py](backend/api/routes/folders.py)).
- Some limited helper reuse exists, for example filter-clause builder in [media.py](backend/api/routes/media.py) and identity helpers in [backend/database/identity.py](backend/database/identity.py).

Error handling:
- Consistent mechanism: `HTTPException` with status/detail across routes.
- Inconsistent shape/content of success/error payloads across endpoints (`status: ok` dicts vs domain object returns vs mixed envelopes).

Response formatting:
- No universal response envelope/middleware.
- Route-specific dict payloads are common, e.g. [pipeline.py](backend/api/routes/pipeline.py), [settings.py](backend/api/routes/settings.py), [writeback.py](backend/api/routes/writeback.py).

Logging:
- Logger setup exists in many route modules, e.g. [pipeline.py](backend/api/routes/pipeline.py#L39), [persons.py](backend/api/routes/persons.py#L29), [admin.py](backend/api/routes/admin.py#L37).
- Logging density is uneven: operational routes like pipeline/admin have substantial logs; many read-heavy endpoints log little.

Shared vs duplicated summary:
- Shared: db context manager, websocket broadcast module, a few identity/centroid helpers.
- Duplicated: inline SQL orchestration, multi-step person/cluster mutation flows, response dict patterns, modal-like business-result formatting per route.

# Duplication Hotspots (Top 5)

1. People/cluster mutation workflow duplicated across route handlers:
- [backend/api/routes/persons.py](backend/api/routes/persons.py)
- [backend/api/routes/faces.py](backend/api/routes/faces.py)
- Repeated sequence: validate entities, update face/cluster/person links, queue writeback, recompute centroid/cooccurrence, broadcast events.

2. Modal implementation duplication across frontend:
- [frontend/src/pages/PeoplePage.tsx](frontend/src/pages/PeoplePage.tsx)
- [frontend/src/pages/QualityPage.tsx](frontend/src/pages/QualityPage.tsx)
- [frontend/src/pages/AdminPage.tsx](frontend/src/pages/AdminPage.tsx)
- [frontend/src/App.tsx](frontend/src/App.tsx)
- Same overlay/container pattern repeated with slight style drift.

3. Tag/discovery list UX overlap:
- [frontend/src/pages/DiscoverPage.tsx](frontend/src/pages/DiscoverPage.tsx)
- [frontend/src/pages/TagsPage.tsx](frontend/src/pages/TagsPage.tsx)
- Both perform very similar top-tag fetch/filter/render behavior.

4. Search-like result tile rendering duplicated:
- [frontend/src/pages/SearchPage.tsx#L159](frontend/src/pages/SearchPage.tsx#L159)
- [frontend/src/pages/AssistantPage.tsx#L483](frontend/src/pages/AssistantPage.tsx#L483)

5. Pipeline monitor duplication:
- [frontend/src/components/PipelinePanel.tsx](frontend/src/components/PipelinePanel.tsx)
- [frontend/src/pages/PipelinePage.tsx](frontend/src/pages/PipelinePage.tsx)
- Both own websocket connection/status/log display and control actions.

# What is Missing That Should Exist

1. Shared Modal/Dialog component:
- Needed based on repeated overlay/modal markup in [App](frontend/src/App.tsx), [PeoplePage](frontend/src/pages/PeoplePage.tsx), [QualityPage](frontend/src/pages/QualityPage.tsx), [AdminPage](frontend/src/pages/AdminPage.tsx).

2. Shared async state wrapper (loading/error/empty):
- Needed because each page hand-rolls similar state rendering logic; examples in [DashboardPage](frontend/src/pages/DashboardPage.tsx), [WritebackPage](frontend/src/pages/WritebackPage.tsx), [QualityPage](frontend/src/pages/QualityPage.tsx), [PhotoGrid](frontend/src/components/PhotoGrid.tsx).

3. Shared UI primitives for Button/Card/Stat:
- Needed due duplicated stat cards and repeated button class strings; see [AdminPage](frontend/src/pages/AdminPage.tsx#L197), [DashboardPage](frontend/src/pages/DashboardPage.tsx#L5).

4. Shared websocket hook/service:
- Needed due duplicated connection lifecycle in [App](frontend/src/App.tsx#L678), [PipelinePanel](frontend/src/components/PipelinePanel.tsx#L53), [PipelinePage](frontend/src/pages/PipelinePage.tsx#L23).

5. Backend route-level transaction helper for complex identity mutations:
- Needed for repeated multi-step updates in [persons.py](backend/api/routes/persons.py) and [faces.py](backend/api/routes/faces.py) to reduce drift and partial-update risk.

6. API surface parity check test:
- Needed because one backend endpoint appears unwrapped: [backend/api/routes/pipeline.py#L714](backend/api/routes/pipeline.py#L714) vs [frontend/src/api/client.ts](frontend/src/api/client.ts).
