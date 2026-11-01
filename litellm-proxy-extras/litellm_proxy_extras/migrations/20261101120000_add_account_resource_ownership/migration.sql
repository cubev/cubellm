CREATE TABLE IF NOT EXISTS "LiteLLM_AccountResourceOwnership" (
    "account_id" TEXT NOT NULL,
    "resource_type" TEXT NOT NULL,
    "resource_id" TEXT NOT NULL,
    "route_id" TEXT NOT NULL,
    "owner_application_id" TEXT NOT NULL,
    "provenance" TEXT NOT NULL,
    "creator_key_hash" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "LiteLLM_AccountResourceOwnership_pkey" PRIMARY KEY ("account_id", "resource_type", "resource_id")
);

CREATE INDEX IF NOT EXISTS "idx_account_resource_owner"
    ON "LiteLLM_AccountResourceOwnership" ("owner_application_id");
