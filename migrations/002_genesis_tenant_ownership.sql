BEGIN;
ALTER TABLE genesis_jobs ADD COLUMN IF NOT EXISTS "tenantId" text;
ALTER TABLE genesis_jobs ADD COLUMN IF NOT EXISTS "ownerPrincipalId" text;
CREATE INDEX IF NOT EXISTS genesis_jobs_owner_idx
  ON genesis_jobs("tenantId", "ownerPrincipalId", "createdAt");
COMMIT;
