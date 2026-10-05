-- Final DDL: ARCHITECTURE.md Part I section 5 merged with Part II section 26.5.
-- Idempotent (IF NOT EXISTS). Forward FK references are allowed by SQLite at CREATE time.
-- Keep CHECK value lists in sync with corpdisc/models/domain.py enums.

CREATE TABLE IF NOT EXISTS runs (
  run_id        INTEGER PRIMARY KEY,
  root_name     TEXT NOT NULL,
  root_url      TEXT NOT NULL,
  root_code     TEXT NOT NULL,          -- e.g. CSI, used as Tree ID prefix
  config_json   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN (
      'RUNNING','PAUSED','BUDGET_EXHAUSTED','COMPLETED','FAILED','CANCELLED')),
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  llm_token_budget INTEGER,
  llm_tokens_used  INTEGER NOT NULL DEFAULT 0,
  llm_cost_usd     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entities (
  entity_id           INTEGER PRIMARY KEY,
  run_id              INTEGER NOT NULL REFERENCES runs(run_id),
  name                TEXT NOT NULL,
  normalized_name     TEXT NOT NULL,
  entity_type         TEXT NOT NULL CHECK (entity_type IN (
      'Parent Company','Operating Group','Business Group','Portfolio Group',
      'Portfolio Company','Subsidiary','Company','Division','Business Unit',
      'Brand','Acquired Company','Unknown')),
  description         TEXT,
  country             TEXT,
  industry            TEXT,
  operating_status    TEXT NOT NULL DEFAULT 'Unknown' CHECK (operating_status IN ('Active','Unknown')),
  verification_status TEXT NOT NULL CHECK (verification_status IN ('CONFIRMED','REVIEW_REQUIRED','REJECTED')),
  classification_confidence REAL,
  confidence          REAL,              -- entity-level, see section 7
  primary_website_id  INTEGER REFERENCES websites(website_id),
  crawl_state         TEXT NOT NULL DEFAULT 'PENDING' CHECK (crawl_state IN (
      'PENDING','EXPANDED','TERMINAL','NO_WEBSITE','UNREACHABLE','BUDGET_STOPPED','SKIPPED')),
  discovery_method    TEXT,              -- DOM_LIST, LLM_TEXT, FOOTER_STATEMENT, SEARCH, ...
  discovery_seq       INTEGER NOT NULL,  -- global monotonic discovery order
  sibling_order       INTEGER,           -- position on the source page
  level               INTEGER,           -- set at finalize
  tree_id             TEXT,              -- set at finalize
  hierarchy_path      TEXT,              -- set at finalize
  merged_into_id      INTEGER REFERENCES entities(entity_id),
  discovered_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(run_id, normalized_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_tree ON entities(run_id, tree_id) WHERE tree_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias_id        INTEGER PRIMARY KEY,
  entity_id       INTEGER NOT NULL REFERENCES entities(entity_id),
  alias           TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source_page_id  INTEGER REFERENCES crawl_pages(page_id),
  UNIQUE (entity_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS websites (
  website_id       INTEGER PRIMARY KEY,
  entity_id        INTEGER NOT NULL REFERENCES entities(entity_id),
  url              TEXT NOT NULL,         -- canonical form
  domain           TEXT NOT NULL,
  registrable_domain TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('OFFICIAL','SECTION_OF_PARENT','EXTERNAL_PROFILE')),
  resolution_method TEXT NOT NULL,        -- LINKED_ON_PARENT, SEARCH_OFFICIAL, FOOTER, ...
  reachable        INTEGER,               -- 1/0/NULL unknown
  last_http_status INTEGER,
  last_checked_at  TEXT,
  UNIQUE (entity_id, url)
);
CREATE INDEX IF NOT EXISTS idx_websites_domain ON websites(registrable_domain);

CREATE TABLE IF NOT EXISTS relationships (
  relationship_id    INTEGER PRIMARY KEY,
  parent_id          INTEGER NOT NULL REFERENCES entities(entity_id),
  child_id           INTEGER NOT NULL REFERENCES entities(entity_id),
  relationship_type  TEXT NOT NULL CHECK (relationship_type IN (
      'Owned by','Part of','Subsidiary of','Operating under','Division of',
      'Portfolio company of','Acquired by','Member of','Managed by','Unknown')),
  verification_status TEXT NOT NULL CHECK (verification_status IN ('CONFIRMED','REVIEW_REQUIRED','REJECTED')),
  confidence         REAL NOT NULL,
  is_primary         INTEGER NOT NULL DEFAULT 1,  -- exactly one primary parent per confirmed child
  source_page_id     INTEGER REFERENCES crawl_pages(page_id),
  discovery_method   TEXT,
  created_at         TEXT NOT NULL,
  CHECK (parent_id <> child_id),
  UNIQUE (parent_id, child_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_primary_parent
  ON relationships(child_id) WHERE is_primary = 1 AND verification_status = 'CONFIRMED';

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id      INTEGER PRIMARY KEY,
  entity_id        INTEGER NOT NULL REFERENCES entities(entity_id),
  relationship_id  INTEGER REFERENCES relationships(relationship_id),
  page_id          INTEGER REFERENCES crawl_pages(page_id),
  url              TEXT NOT NULL,
  quote            TEXT NOT NULL,
  quote_verified   INTEGER NOT NULL,        -- 1 if found in stored page text
  source_class     TEXT NOT NULL CHECK (source_class IN (
      'FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','FIRST_PARTY_OTHER','OFFICIAL_DOCUMENT','THIRD_PARTY')),
  supports         TEXT NOT NULL CHECK (supports IN ('OWNERSHIP','EXISTENCE','WEBSITE','CLASSIFICATION')),
  invocation_id    INTEGER REFERENCES agent_invocations(invocation_id),
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_pages (
  page_id          INTEGER PRIMARY KEY,
  run_id           INTEGER NOT NULL REFERENCES runs(run_id),
  url              TEXT NOT NULL,            -- as requested
  canonical_url    TEXT NOT NULL,
  final_url        TEXT,
  domain           TEXT NOT NULL,
  entity_id        INTEGER REFERENCES entities(entity_id),         -- entity being researched
  parent_entity_id INTEGER REFERENCES entities(entity_id),
  page_type        TEXT,                     -- HOME, ABOUT, COMPANIES, PORTFOLIO, ...
  http_status      INTEGER,
  fetch_method     TEXT CHECK (fetch_method IN ('requests','playwright','cache')),
  content_hash     TEXT,
  text_path        TEXT,                     -- cleaned text on disk under .cache/
  html_path        TEXT,
  robots_allowed   INTEGER,
  pages_found      INTEGER DEFAULT 0,        -- links discovered
  entities_found   INTEGER DEFAULT 0,        -- candidates extracted
  crawl_status     TEXT NOT NULL CHECK (crawl_status IN (
      'OK','CACHED','BLOCKED_ROBOTS','HTTP_ERROR','TIMEOUT','SSL_ERROR','SKIPPED_LIMIT','SKIPPED_FILTER')),
  error            TEXT,
  crawled_at       TEXT NOT NULL,
  UNIQUE (run_id, canonical_url)             -- this is visited_urls
);

CREATE TABLE IF NOT EXISTS domain_stats (    -- this is visited_domains + politeness state
  domain           TEXT PRIMARY KEY,
  robots_txt       TEXT,
  robots_fetched_at TEXT,
  crawl_delay_s    REAL,
  pages_fetched    INTEGER NOT NULL DEFAULT 0,
  error_count      INTEGER NOT NULL DEFAULT 0,
  last_request_at  TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id     INTEGER PRIMARY KEY,
  run_id           INTEGER NOT NULL REFERENCES runs(run_id),
  source_page_id   INTEGER NOT NULL REFERENCES crawl_pages(page_id),
  parent_entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
  raw_name         TEXT NOT NULL,
  normalized_name  TEXT NOT NULL,            -- needed by the section 25.7 unique key
  raw_link_url     TEXT,
  context_snippet  TEXT,
  dom_hint         TEXT,                     -- card, list_item, logo_alt, footer, jsonld
  candidate_kind   TEXT,                     -- COMPANY, GROUP, BRAND, PRODUCT, SERVICE, INDUSTRY,
                                             -- SOLUTION, TECHNOLOGY, FEATURE, LOCATION, OTHER
  proposed_type    TEXT,
  decision         TEXT CHECK (decision IN ('ACCEPTED','REJECTED_NOT_ENTITY','REVIEW','DUPLICATE')),
  resolved_entity_id INTEGER REFERENCES entities(entity_id),
  reason           TEXT,
  confidence       REAL,
  extraction_invocation_id INTEGER REFERENCES agent_invocations(invocation_id),
  gate_code        TEXT,                     -- set when rejected by a gate
  UNIQUE (run_id, source_page_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS research_queue (  -- this is visited_entities + work queue
  task_id          INTEGER PRIMARY KEY,
  run_id           INTEGER NOT NULL REFERENCES runs(run_id),
  entity_id        INTEGER NOT NULL REFERENCES entities(entity_id),
  start_url        TEXT,
  depth            INTEGER NOT NULL,
  priority         INTEGER NOT NULL,         -- depth * 1e6 + discovery_seq
  status           TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','DONE','FAILED','SKIPPED')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  enqueued_at      TEXT NOT NULL,
  started_at       TEXT,
  finished_at      TEXT,
  error            TEXT,
  not_before       TEXT,                     -- per-domain cool-down
  UNIQUE (run_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_ready ON research_queue(run_id, status, priority);

CREATE TABLE IF NOT EXISTS crawl_errors (
  error_id    INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(run_id),
  page_id     INTEGER REFERENCES crawl_pages(page_id),
  entity_id   INTEGER REFERENCES entities(entity_id),
  url         TEXT,
  stage       TEXT NOT NULL,                 -- FETCH, PARSE, LLM, SEARCH, REGISTRY, EXPORT
  error_type  TEXT NOT NULL,
  message     TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (       -- audit log and response cache
  call_id       INTEGER PRIMARY KEY,
  run_id        INTEGER REFERENCES runs(run_id),
  task_type     TEXT NOT NULL,
  model         TEXT NOT NULL,
  prompt_hash   TEXT NOT NULL,               -- hash of task_type + model + schema version + input
  request_json  TEXT NOT NULL,
  response_json TEXT,
  valid         INTEGER NOT NULL,            -- passed Pydantic validation
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  latency_ms    INTEGER,
  created_at    TEXT NOT NULL,
  invocation_id INTEGER REFERENCES agent_invocations(invocation_id),
  UNIQUE (prompt_hash)
);

CREATE TABLE IF NOT EXISTS review_items (
  review_id            INTEGER PRIMARY KEY,
  run_id               INTEGER NOT NULL REFERENCES runs(run_id),
  entity_id            INTEGER NOT NULL REFERENCES entities(entity_id),
  possible_parent_id   INTEGER REFERENCES entities(entity_id),
  reason_code          TEXT NOT NULL,        -- see ReviewReason in models/domain.py
  reason_text          TEXT NOT NULL,
  recommended_action   TEXT NOT NULL,
  confidence           REAL,
  created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_findings (
  finding_id      INTEGER PRIMARY KEY,
  run_id          INTEGER NOT NULL REFERENCES runs(run_id),
  check_name      TEXT NOT NULL,             -- DUPLICATE, BROKEN_LINK, MISSING_PARENT, ORPHAN,
                                             -- MISCLASSIFIED, CYCLE, LOW_CONFIDENCE
  severity        TEXT NOT NULL CHECK (severity IN ('INFO','WARN','ERROR')),
  entity_id       INTEGER REFERENCES entities(entity_id),
  relationship_id INTEGER REFERENCES relationships(relationship_id),
  details         TEXT NOT NULL,
  action_taken    TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_invocations (
  invocation_id  INTEGER PRIMARY KEY,
  run_id         INTEGER NOT NULL REFERENCES runs(run_id),
  task_id        INTEGER REFERENCES research_queue(task_id),
  agent          TEXT NOT NULL CHECK (agent IN ('supervisor_advisor','discovery','extraction',
                   'relationship_verification','website_resolution','deduplication','validation')),
  correlation_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  attempt        INTEGER NOT NULL DEFAULT 1,
  input_hash     TEXT NOT NULL,            -- cache key
  request_json   TEXT NOT NULL,
  response_json  TEXT,
  status         TEXT NOT NULL CHECK (status IN ('OK','PARTIAL','FAILED','SKIPPED')),
  failure_kind   TEXT CHECK (failure_kind IN ('TRANSPORT','SCHEMA','GROUNDING','BUDGET','CIRCUIT_OPEN','TOOL')),
  cache_hit      INTEGER NOT NULL DEFAULT 0,
  tokens_in      INTEGER, tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER,
  started_at     TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invocations_cache
  ON agent_invocations(agent, schema_version, prompt_version, input_hash)
  WHERE status = 'OK';

CREATE TABLE IF NOT EXISTS agent_tool_calls (
  tool_call_id   INTEGER PRIMARY KEY,
  invocation_id  INTEGER NOT NULL REFERENCES agent_invocations(invocation_id),
  tool           TEXT NOT NULL,
  args_json      TEXT NOT NULL,
  allowed        INTEGER NOT NULL,
  result_summary TEXT,
  duration_ms    INTEGER,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_rejections (
  rejection_id   INTEGER PRIMARY KEY,
  run_id         INTEGER NOT NULL REFERENCES runs(run_id),
  invocation_id  INTEGER REFERENCES agent_invocations(invocation_id),
  gate_code      TEXT NOT NULL,            -- G1 ... G14
  item_ref       TEXT NOT NULL,            -- local_ref, link_id, url, pair_id ...
  detail         TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
