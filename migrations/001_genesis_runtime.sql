BEGIN;

CREATE TABLE IF NOT EXISTS genesis_jobs (
  id text PRIMARY KEY,
  "agentSlug" text NOT NULL,
  "buyerWalletId" text,
  "buyerClientId" text,
  "tenantId" text,
  "ownerPrincipalId" text,
  prompt text NOT NULL DEFAULT '',
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL,
  "priceTierCents" integer,
  "idempotencyKey" text UNIQUE,
  "webhookUrl" text,
  "webhookSecret" text,
  "escrowId" text,
  "outputArtifactUris" text[] NOT NULL DEFAULT '{}',
  "resultSummary" text,
  "errorCode" text,
  "errorMessage" text,
  "startedAt" timestamptz,
  "completedAt" timestamptz,
  "lastHeartbeatAt" timestamptz,
  "createdAt" timestamptz NOT NULL DEFAULT now(),
  "updatedAt" timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS genesis_job_events (
  id text PRIMARY KEY, "jobId" text NOT NULL REFERENCES genesis_jobs(id),
  "eventType" text NOT NULL, "fromStatus" text, "toStatus" text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  "createdAt" timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS genesis_agent_sessions (
  id text PRIMARY KEY, "jobId" text NOT NULL REFERENCES genesis_jobs(id),
  "parentJobId" text, "parentSessionId" text, "agentSlug" text NOT NULL,
  status text NOT NULL, "workspaceRoot" text, "artifactUris" text[] NOT NULL DEFAULT '{}',
  "traceJson" jsonb, error text, "startedAt" timestamptz NOT NULL DEFAULT now(),
  "finishedAt" timestamptz, "createdAt" timestamptz NOT NULL DEFAULT now(),
  "updatedAt" timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS genesis_agent_events (
  id text PRIMARY KEY, "jobId" text NOT NULL, "sessionId" text,
  "eventType" text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  "createdAt" timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS genesis_job_relationships (
  id text PRIMARY KEY, "parentJobId" text NOT NULL, "childJobId" text UNIQUE NOT NULL,
  "parentSessionId" text, "childSessionId" text,
  "parentAgentSlug" text, "childAgentSlug" text, "delegationStatus" text NOT NULL,
  "createdAt" timestamptz NOT NULL DEFAULT now(), "updatedAt" timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS genesis_artifacts (
  id text PRIMARY KEY, "jobId" text NOT NULL, "sessionId" text,
  "agentSlug" text, path text NOT NULL, filename text NOT NULL, "mimeType" text,
  "sizeBytes" bigint, sha256 text, "storageBackend" text, uri text,
  "signedUrl" text, "createdAt" timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS genesis_jobs_status_idx ON genesis_jobs(status);
CREATE INDEX IF NOT EXISTS genesis_events_job_idx ON genesis_job_events("jobId", "createdAt");
COMMIT;
